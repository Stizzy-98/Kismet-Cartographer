"""Per-file orchestration shared by export_kismet.py and ingest_kismet.py."""
from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import SCHEMA_VERSION, __version__
from .bulk import BulkIndexer
from .config import REPO_ROOT
from .constants import INDEX_SYSTEM, index_for
from .esclient import Client, EsError
from .kismetdb import KismetDb, file_sha256
from .normalize import (FileContext, TableStats, _base, alert_docs, data_docs, datasource_docs, device_docs,
                        iso, message_docs, packet_docs, snapshot_docs)

# Order matters: datasources fill the datasource name map, devices fill the MAC->device map,
# and both are used by everything after them.
ALL_TABLES = ("datasources", "devices", "packets", "data", "alerts", "messages", "snapshots")
_BUILDERS = {
    "datasources": lambda ctx, st, lim: datasource_docs(ctx, st),
    "devices": device_docs,
    "packets": packet_docs,
    "data": data_docs,
    "alerts": alert_docs,
    "messages": message_docs,
    "snapshots": snapshot_docs,
}


@dataclass
class Options:
    tables: Tuple[str, ...] = ALL_TABLES
    limit: Optional[int] = None          # max rows per table (testing / dry runs)
    rejects_dir: Optional[Path] = REPO_ROOT / "out" / "rejects"
    quiet: bool = False


@dataclass
class FileResult:
    name: str
    sha256: str = ""
    status: str = "pending"              # complete | partial | failed | skipped
    docs_built: int = 0
    docs_indexed: int = 0
    docs_failed: int = 0
    seconds: float = 0.0
    error: str = ""
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rejects_file: str = ""


class RejectLog:
    """Lazily-created NDJSON file of everything that could not be parsed or indexed."""

    def __init__(self, base: Optional[Path], file_name: str, run_id: str):
        self.path: Optional[Path] = None
        self._fh = None
        self._file_name = file_name
        if base:
            base.mkdir(parents=True, exist_ok=True)
            self.path = base / f"{file_name}.{run_id[:8]}.rejects.ndjson"

    def write(self, rec: Dict[str, Any]) -> None:
        if not self.path:
            return
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        rec = {"file": self._file_name, **rec}
        self._fh.write(json.dumps(rec, ensure_ascii=True) + "\n")

    def table_sink(self, table: str, rowid: Any, msg: str) -> None:
        self.write({"stage": "parse", "table": table, "rowid": rowid, "error": msg})

    def close(self) -> Optional[str]:
        if self._fh:
            self._fh.close()
            return str(self.path)
        return None


class Progress:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._last = 0.0
        self._t0 = time.time()
        self._tty = sys.stderr.isatty()

    def tick(self, label: str, docs: int, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.time()
        if not force and now - self._last < (1.0 if self._tty else 15.0):
            return
        self._last = now
        rate = docs / max(now - self._t0, 1e-6)
        msg = f"  {label}: {docs:,} docs ({rate:,.0f}/s)"
        if self._tty:
            sys.stderr.write("\r" + msg.ljust(78))
            if force:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def note(self, msg: str) -> None:
        if self.enabled:
            if self._tty:
                sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()


def iter_documents(db: KismetDb, sha256: str, run_id: str, opts: Options, stats: Dict[str, TableStats],
                   progress: Optional[Progress] = None) -> Iterator[Tuple[str, str, str, Dict[str, Any]]]:
    """Yield (index_alias, _id, record, document) for every source row of the selected tables."""
    ctx = FileContext(db, sha256, run_id)
    for table in ALL_TABLES:
        if table not in opts.tables or not db.has(table):
            continue
        st = stats[table]
        if progress:
            progress.note(f"  reading {table} ...")
        for record, doc_id, doc in _BUILDERS[table](ctx, st, opts.limit):
            yield index_for(record), doc_id, record, doc


def new_stats(rejects: RejectLog) -> Dict[str, TableStats]:
    return {t: TableStats(t, rejects.table_sink) for t in ALL_TABLES}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def marker_id(ctx_hash: str) -> str:
    """Id of the per-file completion marker (deterministic, so re-runs update it in place)."""
    import base64
    import hashlib
    h = hashlib.sha256(f"{ctx_hash}|ingest.run".encode()).digest()[:15]
    return base64.urlsafe_b64encode(h).decode()


def is_complete(client: Client, sha256: str) -> bool:
    try:
        r = client.get(f"/{INDEX_SYSTEM}/_doc/{marker_id(sha256)}", params={"_source_includes": "kismet.ingest,kismet.record"})
    except EsError as e:
        if e.status == 404:
            return False
        raise
    ing = (r.get("_source") or {}).get("kismet", {}).get("ingest", {})
    return bool(r.get("found")) and ing.get("status") == "complete" and ing.get("schema_version") == SCHEMA_VERSION


def purge_file(client: Client, sha256: str) -> int:
    """Delete every document that came from this file content (used by --purge)."""
    r = client.post("/kismet-cartographer-*/_delete_by_query",
                    {"query": {"term": {"kismet.source.file_hash": sha256}}},
                    params={"conflicts": "proceed", "wait_for_completion": True, "refresh": False,
                            "expand_wildcards": "open"})
    return int(r.get("deleted", 0))


def ingest_file(client: Client, path: str, opts: Options, *, workers: int = 2, batch_docs: int = 5000,
                op: str = "index", skip_complete: bool = False, purge: bool = False) -> FileResult:
    t0 = time.time()
    name = Path(path).name
    res = FileResult(name=name)
    progress = Progress(not opts.quiet)
    run_id = str(uuid.uuid4())
    rejects = RejectLog(opts.rejects_dir, name, run_id)
    bulk: Optional[BulkIndexer] = None
    try:
        db = KismetDb(path)
    except ValueError as e:
        res.status, res.error = "failed", str(e)
        return res
    try:
        res.sha256 = sha256 = file_sha256(path)
        if skip_complete and is_complete(client, sha256):
            res.status = "skipped"
            progress.note(f"  {name}: already ingested (marker present) - skipping")
            return res
        if purge:
            n = purge_file(client, sha256)
            progress.note(f"  purged {n:,} existing documents for this file")
        stats = new_stats(rejects)
        started = _utcnow()
        bulk = BulkIndexer(client, workers=workers, batch_docs=batch_docs, op=op, reject_sink=lambda r: rejects.write({"stage": "index", **r}))
        for index, doc_id, record, doc in iter_documents(db, sha256, run_id, opts, stats, progress):
            bulk.add(index, doc_id, doc)
            res.docs_built += 1
            if res.docs_built % 2000 == 0:
                progress.tick(name, res.docs_built)
        bulk.flush()
        progress.tick(name, res.docs_built, force=True)
        res.docs_indexed, res.docs_failed = bulk.indexed + bulk.existing, bulk.failed
        res.tables = {t: s.as_dict() for t, s in stats.items() if s.rows or s.docs}
        parse_errors = sum(s.parse_errors for s in stats.values())
        no_gps = sum(s.no_gps for s in stats.values())
        res.status = "complete" if (res.docs_failed == 0) else "partial"

        # A subset (--tables) or truncated (--limit) run must not leave a "complete" marker: it would make
        # --skip-complete skip a file whose other tables were never (re)written, and shrink the ledger.
        if set(opts.tables) != set(ALL_TABLES) or opts.limit:
            progress.note("  partial run (--tables/--limit): no completion marker written")
            return res
        # Completion marker: also the per-file ledger the validator compares against.
        first, last = db.time_bounds()
        ctx_stub = FileContext(db, sha256, run_id)
        doc = _base(ctx_stub, "ingest.run", "KISMET", 0, "kismet.ingest", kind="state", category=["process"])
        doc["@timestamp"] = _utcnow()
        doc["kismet"]["source"].update({"file_size": db.size, "first_ts": iso(first), "last_ts": iso(last)})
        doc["kismet"]["ingest"].update({
            "status": res.status, "started": started, "finished": _utcnow(),
            "duration_s": round(time.time() - t0, 1), "docs_built": res.docs_built,
            "docs_indexed": res.docs_indexed, "docs_failed": res.docs_failed, "parse_errors": parse_errors,
            "docs_without_gps": no_gps, "options": {"tables": list(opts.tables), "limit": opts.limit, "op": op},
            "tables": res.tables,
        })
        bulk.add(INDEX_SYSTEM, marker_id(sha256), doc)
        bulk.flush()
        if parse_errors or res.docs_failed:
            progress.note(f"  ! {parse_errors:,} unparseable records, {res.docs_failed:,} rejected by Elasticsearch")
            for t, s in stats.items():
                for ex in s.examples[:2]:
                    progress.note(f"    {t}: {ex}")
            for ex in bulk.failure_examples[:3]:
                progress.note(f"    {ex}")
        if parse_errors and res.status == "complete":
            res.status = "complete"  # parse errors keep the doc (with kismet.parse_error) - counted, not fatal
    except KeyboardInterrupt:
        res.status, res.error = "failed", "interrupted"
        raise
    except Exception as e:  # noqa: BLE001
        res.status, res.error = "failed", f"{type(e).__name__}: {e}"
    finally:
        if bulk:
            bulk.close()
        res.rejects_file = rejects.close() or ""
        db.close()
        res.seconds = time.time() - t0
    return res
