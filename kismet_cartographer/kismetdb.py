"""Read-only access to KismetDB (SQLite) files.

Originals are never modified: files are opened with `mode=ro&immutable=1`, which also
stops SQLite from creating -wal/-shm/-journal side files next to them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import REPO_ROOT

KNOWN_TABLES = ("KISMET", "devices", "packets", "data", "datasources", "alerts", "messages", "snapshots")
HASH_CACHE = REPO_ROOT / "state" / "hashes.json"


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def open_readonly(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.text_factory = _decode  # invalid UTF-8 in text columns must not abort a file
    return con


def file_sha256(path: str, use_cache: bool = True) -> str:
    """Streaming SHA-256, cached by (path, size, mtime) so re-runs don't re-read large files."""
    p = Path(path).resolve()
    st = p.stat()
    key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    cache: Dict[str, str] = {}
    if use_cache and HASH_CACHE.is_file():
        try:
            cache = json.loads(HASH_CACHE.read_text())
        except (OSError, ValueError):
            cache = {}
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if use_cache:
        try:
            HASH_CACHE.parent.mkdir(parents=True, exist_ok=True)
            cache[key] = digest
            tmp = HASH_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=1))
            os.replace(tmp, HASH_CACHE)
        except OSError:
            pass
    return digest


class KismetDb:
    def __init__(self, path: str):
        self.path = str(Path(path).resolve())
        self.name = Path(path).name
        self.size = Path(path).stat().st_size
        self.con = open_readonly(path)
        try:
            self.tables = {r[0] for r in self.con.execute("select name from sqlite_master where type='table'")}
        except sqlite3.DatabaseError as e:
            raise ValueError(f"{self.name}: not a readable SQLite/KismetDB file ({e})") from e
        if "KISMET" not in self.tables:
            raise ValueError(f"{self.name}: no KISMET table - not a KismetDB file")
        row = self.con.execute("select kismet_version, db_version, db_module from KISMET limit 1").fetchone()
        self.kismet_version, self.db_version, self.db_module = row if row else (None, None, None)
        self._cols: Dict[str, List[str]] = {}

    def close(self) -> None:
        self.con.close()

    def columns(self, table: str) -> List[str]:
        if table not in self._cols:
            self._cols[table] = [r[1] for r in self.con.execute(f'pragma table_info("{table}")')]
        return self._cols[table]

    def has(self, table: str) -> bool:
        return table in self.tables

    def select(self, table: str, wanted: Sequence[str], limit: Optional[int] = None,
               where: str = "") -> Iterator[Tuple]:
        """Yield (rowid, *wanted) tuples. Columns missing from this DB version come back as None."""
        if not self.has(table):
            return
        present = set(self.columns(table))
        exprs = [(f'"{c}"' if c in present else "NULL") for c in wanted]
        sql = f'select rowid, {", ".join(exprs)} from "{table}"'
        if where:
            sql += f" where {where}"
        sql += " order by rowid"
        if limit:
            sql += f" limit {int(limit)}"
        cur = self.con.execute(sql)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            yield from batch

    def scalar(self, sql: str) -> Any:
        r = self.con.execute(sql).fetchone()
        return r[0] if r else None

    def count(self, table: str) -> int:
        return int(self.scalar(f'select count(*) from "{table}"') or 0) if self.has(table) else 0

    def collector_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """(min_lat, max_lat, min_lon, max_lon) of every real position the *collector* recorded in this
        file (packets + snapshots); None if the capture never had a GPS fix."""
        boxes = []
        for table in ("packets", "snapshots"):
            if self.has(table) and {"lat", "lon"} <= set(self.columns(table)):
                r = self.con.execute(
                    f'select min(lat), max(lat), min(lon), max(lon) from "{table}" where lat is not null and lon is not null '
                    f"and not (lat = 0 and lon = 0) and abs(lat) <= 90 and abs(lon) <= 180").fetchone()
                if r and r[0] is not None:
                    boxes.append(r)
        if not boxes:
            return None
        return (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))

    def time_bounds(self) -> Tuple[Optional[int], Optional[int]]:
        """Cheap first/last timestamps (tables are append-only in time order)."""
        lo: List[int] = []
        hi: List[int] = []
        for table, col in (("packets", "ts_sec"), ("data", "ts_sec"), ("messages", "ts_sec")):
            if self.has(table) and col in self.columns(table):
                a = self.scalar(f'select "{col}" from "{table}" order by rowid limit 1')
                b = self.scalar(f'select "{col}" from "{table}" order by rowid desc limit 1')
                for v, dest in ((a, lo), (b, hi)):
                    if isinstance(v, int) and v > 0:
                        dest.append(v)
        if self.has("devices"):
            a = self.scalar("select min(first_time) from devices where first_time > 0")
            b = self.scalar("select max(last_time) from devices")
            if isinstance(a, int):
                lo.append(a)
            if isinstance(b, int) and b > 0:
                hi.append(b)
        return (min(lo) if lo else None, max(hi) if hi else None)


def blob_json(value: Any) -> Tuple[Optional[Any], Optional[str]]:
    """Parse a JSON BLOB/TEXT column. Returns (obj, None) or (None, error message)."""
    if value is None:
        return None, "null"
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not value.strip():
        return None, "empty"
    try:
        return json.loads(value), None
    except ValueError as e:
        return None, f"invalid JSON: {e}"


_KISMET_FILE_RE = re.compile(r"\.kismet$", re.I)


def find_kismet_files(target: str) -> List[str]:
    p = Path(target)
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        return sorted(str(x) for x in p.iterdir() if x.is_file() and _KISMET_FILE_RE.search(x.name))
    raise FileNotFoundError(target)
