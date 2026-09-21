#!/usr/bin/env python3
"""Inspect KismetDB (.kismet) files without modifying them: versions, schema, tables, datasources,
device/packet mix and the data-quality traps this project defends against.

  inspect_kismet.py wardrives/Kismet-YYYYMMDD-HH-MM-SS-1.kismet
  inspect_kismet.py wardrives/ --summary          # one line per file
  inspect_kismet.py capture.kismet --json --full  # exact GPS/signal stats (scans every packet row)

By default GPS/signal statistics use the first --sample rows of each big table so that even a
1 GB file inspects in seconds; --full scans everything.
Exit codes: 0 ok, 1 a file could not be read, 2 usage error.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.kismetdb import KismetDb, find_kismet_files  # noqa: E402


def gps_quality(db: KismetDb, table: str, limit) -> dict:
    if not db.has(table):
        return {}
    cols = set(db.columns(table))
    if not {"lat", "lon"} <= cols:
        return {}
    lim = f" limit {int(limit)}" if limit else ""
    sig = "sum(signal=0) signal_zero, min(signal) min_signal, max(signal) max_signal," if "signal" in cols else ""
    sql = (f"select count(*), sum(lat is null or lon is null) null_gps, sum(lat=0 and lon=0) zero_zero, "
           f"sum(abs(lat)>90 or abs(lon)>180) out_of_range, {sig} "
           f"sum(lat!=0 and lon!=0 and abs(lat)<=90 and abs(lon)<=180) valid "
           f"from (select * from \"{table}\"{lim})")
    cur = db.con.execute(sql)
    names = [d[0] for d in cur.description]
    return dict(zip(["rows_examined" if n == "count(*)" else n for n in names], cur.fetchone()))


def inspect(path: str, sample, full: bool) -> dict:
    db = KismetDb(path)
    try:
        lim = None if full else sample
        rep = {
            "file": db.name, "bytes": db.size, "kismet_version": db.kismet_version,
            "db_version": db.db_version, "db_module": db.db_module,
            "tables": {}, "schema": {},
        }
        for t in sorted(db.tables):
            rep["schema"][t] = db.columns(t)
            if t != "KISMET":
                rep["tables"][t] = db.count(t)
        rep["time_range"] = dict(zip(("first", "last"), db.time_bounds()))
        if db.has("datasources"):
            rep["datasources"] = [dict(zip(("uuid", "type", "definition", "name", "interface"), r[1:]))
                                  for r in db.select("datasources", ["uuid", "typestring", "definition", "name", "interface"])]
        if db.has("devices"):
            rep["devices_by_phy_type"] = {f"{p}|{t}": n for p, t, n in db.con.execute(
                "select phyname, type, count(*) from devices group by 1,2 order by 3 desc")}
            rep["adsb_devices_with_position"] = db.scalar(
                "select count(*) from devices where phyname='ADSB' and json_extract(device,"
                "'$.\"kismet.device.base.location\".\"kismet.common.location.last\".\"kismet.common.location.geopoint\"[0]')!=0") or 0
        if db.has("packets"):
            rep["packets_by_phy"] = {p: n for p, n in db.con.execute(
                "select phyname, count(*) from (select phyname from packets" +
                (f" limit {int(lim)}" if lim else "") + ") group by 1")}
            rep["packets_gps"] = gps_quality(db, "packets", lim)
        if db.has("data"):
            rep["data_types"] = {f"{t}|{p}": n for t, p, n in db.con.execute("select type, phyname, count(*) from data group by 1,2")}
            rep["data_gps"] = gps_quality(db, "data", lim)
        for t, col in (("snapshots", "snaptype"), ("messages", "msgtype"), ("alerts", "header")):
            if db.has(t):
                rep[f"{t}_by_{col}"] = {k or "": n for k, n in db.con.execute(f'select "{col}", count(*) from "{t}" group by 1')}
        rep["sampled"] = None if full else sample
        return rep
    finally:
        db.close()


def print_text(r: dict) -> None:
    print(f"== {r['file']}  ({r['bytes'] / 1e6:,.1f} MB)  Kismet {r['kismet_version']}  db v{r['db_version']} ({r['db_module']})")
    print("   rows: " + ", ".join(f"{t}={n:,}" for t, n in r["tables"].items()))
    tr = r["time_range"]
    print(f"   time range (epoch): {tr['first']} .. {tr['last']}")
    for d in r.get("datasources", []):
        print(f"   datasource: {d['name']} type={d['type']} iface={d['interface']} uuid={d['uuid']}")
    if r.get("devices_by_phy_type"):
        print("   devices: " + ", ".join(f"{k}={v:,}" for k, v in r["devices_by_phy_type"].items()))
        print(f"   ADS-B devices with a real position: {r['adsb_devices_with_position']:,}")
    for key in ("packets_gps", "data_gps"):
        g = r.get(key)
        if g:
            n = g["rows_examined"] or 1
            print(f"   {key}: {g['valid']:,} valid, {g['zero_zero']:,} are (0,0)=no fix ({100 * g['zero_zero'] / n:.1f}%), "
                  f"{g['out_of_range']:,} out of range, {g['null_gps']:,} null"
                  + (f"; signal==0 (absent): {g.get('signal_zero', 0):,}" if 'signal_zero' in g else "")
                  + (f"  [first {r['sampled']:,} rows]" if r["sampled"] else ""))
    for k in ("data_types", "snapshots_by_snaptype", "messages_by_msgtype", "alerts_by_header"):
        if r.get(k):
            print(f"   {k}: " + ", ".join(f"{a}={b:,}" for a, b in r[k].items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--summary", action="store_true", help="one line per file")
    ap.add_argument("--full", action="store_true", help="scan every row for GPS/signal stats")
    ap.add_argument("--sample", type=int, default=200_000, help="rows examined per big table when not --full")
    ap.add_argument("--schema", action="store_true", help="also print every table's columns")
    args = ap.parse_args()
    try:
        files = find_kismet_files(args.target)
    except FileNotFoundError:
        print(f"not found: {args.target}", file=sys.stderr)
        return 2
    if not files:
        print("no .kismet files found", file=sys.stderr)
        return 2
    rc, out = 0, []
    for f in files:
        try:
            r = inspect(f, args.sample, args.full)
        except (ValueError, Exception) as e:  # noqa: BLE001
            print(f"!! {Path(f).name}: {e}", file=sys.stderr)
            rc = 1
            continue
        out.append(r)
        if args.json:
            continue
        if args.summary:
            t = r["tables"]
            print(f"{r['file']}  v{r['kismet_version']}  packets={t.get('packets', 0):,} devices={t.get('devices', 0):,} "
                  f"data={t.get('data', 0):,} alerts={t.get('alerts', 0):,}")
        else:
            print_text(r)
            if args.schema:
                for t, cols in r["schema"].items():
                    print(f"   schema {t}: {', '.join(cols)}")
    if args.json:
        json.dump(out if len(out) != 1 else out[0], sys.stdout, indent=2)
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
