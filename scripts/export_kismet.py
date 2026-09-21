#!/usr/bin/env python3
"""Extract + normalise KismetDB file(s) into Elasticsearch bulk NDJSON. No network, no Elastic needed.

  export_kismet.py wardrives/Kismet-YYYYMMDD-HH-MM-SS-1.kismet -o out/
  export_kismet.py wardrives/ -o out/ --tables devices,packets --limit 1000

Writes <out>/<capture>.bulk.ndjson (action line + document line per record; index names are the
write aliases, `_id` is the deterministic id) that can be loaded with `curl -X POST _bulk` or
inspected offline. The .kismet originals are opened read-only/immutable and never modified.

Exit codes: 0 ok, 1 some records unparseable / file failed, 2 usage error.
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.ingest import ALL_TABLES, Options, RejectLog, iter_documents, new_stats  # noqa: E402
from kismet_cartographer.kismetdb import KismetDb, file_sha256, find_kismet_files  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help=".kismet file or a directory of them")
    ap.add_argument("-o", "--out", default=str(ROOT / "out" / "export"), help="output directory (default out/export)")
    ap.add_argument("--tables", default=",".join(ALL_TABLES), help=f"comma list of: {', '.join(ALL_TABLES)}")
    ap.add_argument("--limit", type=int, default=None, help="max rows per table (testing)")
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
        print(f"no .kismet files in {args.target}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    opts = Options(tables=tables, limit=args.limit, quiet=args.quiet, rejects_dir=out / "rejects")

    failed = 0
    for i, f in enumerate(files, 1):
        name = Path(f).name
        print(f"[{i}/{len(files)}] {name}", file=sys.stderr)
        t0 = time.time()
        run_id = str(uuid.uuid4())
        rejects = RejectLog(opts.rejects_dir, name, run_id)
        try:
            db = KismetDb(f)
        except ValueError as e:
            print(f"  ! {e}", file=sys.stderr)
            failed += 1
            continue
        try:
            sha = file_sha256(f)
            stats = new_stats(rejects)
            dest = out / f"{name}.bulk.ndjson"
            n = 0
            with open(dest, "w", encoding="utf-8") as fh:
                for index, doc_id, record, doc in iter_documents(db, sha, run_id, opts, stats):
                    fh.write(json.dumps({"index": {"_index": index, "_id": doc_id}}, separators=(",", ":")) + "\n")
                    fh.write(json.dumps(doc, separators=(",", ":")) + "\n")
                    n += 1
            errs = sum(s.parse_errors for s in stats.values())
            print(f"  -> {dest}  {n:,} docs in {time.time() - t0:.1f}s; unparseable records: {errs:,}", file=sys.stderr)
            for t, s in stats.items():
                if s.rows:
                    print(f"     {t:<12} rows={s.rows:<9,} docs={s.docs:<9,} with_gps={s.geo_ok:<9,} "
                          f"no_gps={s.no_gps:<9,} invalid_gps={s.invalid_gps:<4} parse_errors={s.parse_errors}", file=sys.stderr)
                    for ex in s.examples[:2]:
                        print(f"        e.g. {ex}", file=sys.stderr)
            failed += 1 if errs else 0
        finally:
            rejects.close()
            db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
