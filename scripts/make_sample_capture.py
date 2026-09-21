#!/usr/bin/env python3
"""Write a small INVENTED Kismet capture, so you can try the installer and the dashboards before you have data of your own.

  python3 scripts/make_sample_capture.py                          # -> wardrives/sample-capture.kismet
  python3 scripts/make_sample_capture.py -o /tmp/try.kismet

Nothing in it is real: made-up brands, networks and aircraft along an arbitrary route. It has Kismet's real table layout and
includes the awkward cases the pipeline handles (no GPS fix, signal 0, garbage positions, malformed rows).

It is loaded like any other capture, so it shows up in your indices and dashboards: delete the file (and, if you loaded it,
its documents: `ingest_kismet.py <file> --purge` replaces them, `docs/operations.md` shows how to remove a capture) when done.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer import synthetic  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=str(ROOT / "wardrives" / "sample-capture.kismet"))
    args = ap.parse_args()
    out = Path(args.out)
    counts = synthetic.build(out)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB)")
    for table, n in counts.items():
        print(f"  {table:<12} {n:>5}")
    print("An invented capture: do not mix it into data you care about.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
