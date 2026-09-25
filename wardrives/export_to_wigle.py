#!/usr/bin/env python3
"""Turn every .kismet file in this folder into one WiGLE CSV, ready to upload at https://wigle.net/uploads

Run it from anywhere - it always looks at the folder it lives in, not your current directory:

  ./wardrives/export_to_wigle.py

A thin convenience wrapper around kismet_cartographer.wigle (the same code scripts/export_wigle.py uses),
scoped to this one folder so there is nothing to point it at. Reads .kismet files read-only/immutable and
never modifies or deletes them. See docs/ingestion.md for what does and does not end up in the CSV (one row
per access point/SSID at the collector position where it was heard strongest; client devices are not
exported).

Exit codes: 0 ok, 1 nothing to export.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer import wigle  # noqa: E402
from kismet_cartographer.kismetdb import find_kismet_files  # noqa: E402


def main() -> int:
    files = find_kismet_files(str(HERE))
    if not files:
        print(f"no .kismet files in {HERE}", file=sys.stderr)
        return 1
    out = HERE / "wigle_export.csv"
    stats = wigle.new_stats()
    tmp = out.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        wigle.write_csv(fh, files, stats, progress=lambda i, n, name: print(f"[{i}/{n}] {name}", file=sys.stderr))
    tmp.replace(out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    return 0 if stats["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
