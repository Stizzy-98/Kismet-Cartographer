#!/usr/bin/env python3
"""Convert KismetDB capture(s) into one WiGLE CSV (WigleWifi-1.4) for upload at https://wigle.net/uploads

  export_wigle.py wardrives/ -o exports/wigle/kismet-wardrives-wigle.csv

One row per Wi-Fi access point (and per SSID it advertised) per capture file, at the collector position
where that AP was heard with the strongest signal (packets with a real GPS fix only). Client devices are
not exported. The .kismet originals are opened read-only/immutable and never modified or deleted.

Exit codes: 0 ok, 1 nothing to export / file failed, 2 usage error.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer import wigle  # noqa: E402
from kismet_cartographer.kismetdb import find_kismet_files  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help=".kismet file or a directory of them")
    ap.add_argument("-o", "--out", default=str(ROOT / "exports" / "wigle" / "kismet-wardrives-wigle.csv"))
    args = ap.parse_args()
    try:
        files = find_kismet_files(args.target)
    except FileNotFoundError:
        print(f"not found: {args.target}", file=sys.stderr)
        return 2
    if not files:
        print(f"no .kismet files in {args.target}", file=sys.stderr)
        return 2
    out = Path(args.out)
    if out.suffix.lower() == ".kismet":
        print("refusing to write a .kismet output path", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
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
