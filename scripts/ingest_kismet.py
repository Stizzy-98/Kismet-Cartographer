#!/usr/bin/env python3
"""Ingest KismetDB (.kismet) file(s) into Elasticsearch.

  ingest_kismet.py                                   # everything in the wardrives/ folder of this project
  ingest_kismet.py wardrives/Kismet-YYYYMMDD-HH-MM-SS-1.kismet
  ingest_kismet.py wardrives/ --skip-complete --workers 3
  ingest_kismet.py capture.kismet --purge            # replace everything from this file

Safe to run repeatedly: document `_id`s are deterministic (file content hash + table + row), so
re-ingesting the same file overwrites the same documents (--op create skips them instead).
Originals are opened read-only. Unparseable rows are still indexed where possible (flagged
kismet.parse_error) and are always listed in out/rejects/*.ndjson; rejected bulk items likewise.

Credential: ELASTIC_API_KEY (write/read on kismet-cartographer-* is enough) + ELASTIC_URL +
ELASTIC_CA_CERT, from the environment or a protected env file (docs/security.md).

Exit codes: 0 everything indexed, 1 some records rejected/unparseable or a file failed,
2 usage/config error, 3 cannot connect/authenticate.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import add_connection_args, load_config  # noqa: E402
from kismet_cartographer.constants import FAMILIES  # noqa: E402
from kismet_cartographer.esclient import ConnectionFailure, EsError, client_from_config, preflight  # noqa: E402
from kismet_cartographer.ingest import ALL_TABLES, Options, ingest_file  # noqa: E402
from kismet_cartographer.kismetdb import find_kismet_files  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=str(ROOT / "wardrives"),
                    help=".kismet file or a directory of them (default: the wardrives/ folder of this project)")
    ap.add_argument("--tables", default=",".join(ALL_TABLES), help=f"comma list of: {', '.join(ALL_TABLES)}")
    ap.add_argument("--limit", type=int, default=None, help="max rows per table (testing)")
    ap.add_argument("--workers", type=int, default=2, help="parallel bulk requests (default 2; be gentle on shared clusters)")
    ap.add_argument("--batch-docs", type=int, default=5000, help="documents per bulk request")
    ap.add_argument("--op", choices=("index", "create"), default="index", help="index=overwrite (default), create=skip existing")
    ap.add_argument("--skip-complete", action="store_true", help="skip files whose completion marker is already in Elasticsearch")
    ap.add_argument("--purge", action="store_true", help="delete this file's existing documents first")
    ap.add_argument("--rejects-dir", default=str(ROOT / "out" / "rejects"))
    add_connection_args(ap)
    ap.add_argument("--allow-parse-errors", action="store_true",
                    help="exit 0 when the only problem is unparseable source records (they are still indexed and listed in the rejects file)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    tables = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    bad = [t for t in tables if t not in ALL_TABLES]
    if bad:
        print(f"unknown table(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    try:
        files = find_kismet_files(args.target)
    except FileNotFoundError:
        print(f"not found: {args.target}", file=sys.stderr)
        return 2
    if not files:
        print(f"no .kismet files in {args.target}\nCopy your Kismet captures into the wardrives/ folder "
              f"(or pass another file or folder).", file=sys.stderr)
        return 2

    cfg = load_config(args=args)
    try:
        client = client_from_config(cfg, insecure=args.insecure)
        version, missing = preflight(client, list(FAMILIES))
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except ConnectionFailure as e:
        print(f"cannot connect to {cfg.elastic_url}: {e}\n(TLS errors: set ELASTIC_CA_CERT, see docs/troubleshooting.md)", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"authentication/authorisation failed: {e}", file=sys.stderr)
        return 3
    if missing:
        print(f"missing write alias(es): {', '.join(missing)} - run scripts/install_elastic.py first", file=sys.stderr)
        return 2
    print(f"Elasticsearch {version or '(version hidden: key has no cluster monitor)'} @ {cfg.elastic_url}; {len(files)} file(s)", file=sys.stderr)

    opts = Options(tables=tables, limit=args.limit, quiet=args.quiet, rejects_dir=Path(args.rejects_dir))
    t0 = time.time()
    results = []
    for i, f in enumerate(files, 1):
        if not args.quiet:
            print(f"[{i}/{len(files)}] {Path(f).name}", file=sys.stderr)
        try:
            r = ingest_file(client, f, opts, workers=args.workers, batch_docs=args.batch_docs, op=args.op,
                            skip_complete=args.skip_complete, purge=args.purge)
        except KeyboardInterrupt:
            print("\ninterrupted - safe to re-run (deterministic ids)", file=sys.stderr)
            return 1
        except EsError as e:
            print(f"Elasticsearch error: {e}", file=sys.stderr)
            return 3 if e.status in (401, 403) else 1
        except ConnectionFailure as e:
            print(f"connection lost: {e}", file=sys.stderr)
            return 3
        results.append(r)
        print(f"  {r.status.upper():<8} {r.docs_indexed:,} indexed, {r.docs_failed:,} rejected, "
              f"{r.seconds:.0f}s  {('- ' + r.error) if r.error else ''}", file=sys.stderr)

    print("\nSummary", file=sys.stderr)
    tot = {"i": 0, "f": 0}
    for r in results:
        tot["i"] += r.docs_indexed
        tot["f"] += r.docs_failed
        gps = sum(t.get("no_gps", 0) for t in r.tables.values())
        pe = sum(t.get("parse_errors", 0) for t in r.tables.values())
        print(f"  {r.status:<9} {r.name}  docs={r.docs_indexed:,} rejected={r.docs_failed:,} no_gps={gps:,} unparseable={pe:,}"
              + (f"  rejects -> {r.rejects_file}" if r.rejects_file else ""), file=sys.stderr)
    print(f"  total: {tot['i']:,} indexed, {tot['f']:,} rejected in {time.time() - t0:.0f}s", file=sys.stderr)
    bad_files = [r for r in results if r.status in ("failed", "partial")]
    unparseable = sum(sum(t.get("parse_errors", 0) for t in r.tables.values()) for r in results)
    return 1 if (bad_files or (unparseable and not args.allow_parse_errors)) else 0


if __name__ == "__main__":
    sys.exit(main())
