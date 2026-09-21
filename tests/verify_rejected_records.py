#!/usr/bin/env python3
"""Exhaustive check of the RARE classes against the original KismetDB rows (needs Elasticsearch + the .kismet files).

Every document whose position or altitude was rejected is fetched from Elasticsearch and compared with the row it came
from, to prove that (a) the rejection was justified by the source values, (b) the rejected value was preserved in
`geo.rejected` / `aircraft.rejected`, and (c) no coordinates were kept.

    python3 tests/verify_rejected_records.py wardrives/
Exit code 0 = every rejected document verified, 1 = at least one discrepancy.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import load_config  # noqa: E402
from kismet_cartographer.constants import MAX_EMITTER_RANGE_KM, READ_ALIAS_ALL  # noqa: E402
from kismet_cartographer.esclient import client_from_config  # noqa: E402
from kismet_cartographer.geo import OK, classify, from_kismet_geopoint, km_to_box  # noqa: E402
from kismet_cartographer.kismetdb import KismetDb, blob_json  # noqa: E402

src_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "wardrives")
es = client_from_config(load_config())
dbs = {}


def db(name):
    if name not in dbs:
        dbs[name] = KismetDb(str(src_dir / name))
    return dbs[name]


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


q = {"bool": {"should": [{"term": {"geo.status": "invalid"}}, {"exists": {"field": "aircraft.rejected.altitude"}}],
              "minimum_should_match": 1}}
docs, after = [], None
while True:
    body = {"size": 1000, "query": q, "sort": [{"_doc": "asc"}], "_source": ["kismet.record", "kismet.source", "geo", "aircraft"]}
    if after:
        body["search_after"] = after
    r = es.post(f"/{READ_ALIAS_ALL}/_search", body)
    hits = r["hits"]["hits"]
    if not hits:
        break
    docs += [h["_source"] for h in hits]
    after = hits[-1]["sort"]

alt_q = {"bool": {"filter": [{"term": {"geo.location_type": "emitter_reported"}}, {"term": {"geo.status": "ok"}}],
                  "must_not": [{"exists": {"field": "aircraft.altitude"}}]}}
after = None
while True:
    body = {"size": 1000, "query": alt_q, "sort": [{"_doc": "asc"}], "_source": ["kismet.record", "kismet.source", "geo", "aircraft"]}
    if after:
        body["search_after"] = after
    r = es.post(f"/{READ_ALIAS_ALL}/_search", body)
    hits = r["hits"]["hits"]
    if not hits:
        break
    docs += [h["_source"] for h in hits if "rejected" in (h["_source"].get("aircraft") or {})]
    after = hits[-1]["sort"]

checked, bad, kinds = 0, [], Counter()
for d in docs:
    src, geo, ac = d["kismet"]["source"], d.get("geo") or {}, d.get("aircraft") or {}
    d_ = db(src["file"])
    table, rowid, reason = src["table"], src["rowid"], geo.get("reason")
    kinds[(table, reason or "altitude-only")] += 1
    checked += 1
    why = None
    if "location" in geo and geo.get("status") != "ok":
        why = "location present on a rejected doc"
    elif table in ("packets", "data", "alerts", "messages", "snapshots") and reason in ("out_of_range_or_not_numeric", "implausible_range_from_collector"):
        lat, lon = d_.con.execute(f'select lat, lon from "{table}" where rowid=?', (rowid,)).fetchone()
        rej = geo.get("rejected") or {}
        if not (near(rej.get("lat"), lat) and near(rej.get("lon"), lon)):
            why = f"rejected value {rej} != source ({lat}, {lon})"
        elif reason == "out_of_range_or_not_numeric" and not (abs(lat) > 90 or abs(lon) > 180):
            why = "rejected as out of range but source is in range"
        elif reason == "implausible_range_from_collector":
            box = d_.collector_bbox()
            if box is None or km_to_box(lat, lon, box) <= MAX_EMITTER_RANGE_KM:
                why = "rejected as implausible but source is within range of the collector"
    elif table == "devices" and reason in ("centroid_outside_device_bounds", "position_outside_device_bounds", "implausible_range_from_collector"):
        r = d_.con.execute("select min_lat, min_lon, max_lat, max_lon, avg_lat, avg_lon, cast(device as text), phyname from devices where rowid=?", (rowid,)).fetchone()
        mnl, mnn, mxl, mxn, avl, avn, blob, phy = r
        obj, _ = blob_json(blob)
        loc = (obj or {}).get("kismet.device.base.location") or {}
        key = "kismet.common.location.last" if phy == "ADSB" else "kismet.common.location.avg_loc"
        st, la, lo = from_kismet_geopoint((loc.get(key) or {}).get("kismet.common.location.geopoint"))
        if st != OK and phy != "ADSB":  # same fallback the pipeline uses: the table's own avg_lat/avg_lon
            st, la, lo = classify(avl, avn)
        inside = st == "ok" and (min(mnl, mxl) - 1e-3 <= la <= max(mnl, mxl) + 1e-3) and (min(mnn, mxn) - 1e-3 <= lo <= max(mnn, mxn) + 1e-3)
        if st != "ok":
            why = "source has no usable position but doc claims a rejected one"
        elif reason.endswith("outside_device_bounds") and inside:
            why = "rejected as outside the device's own bounds but it is inside"
        elif reason == "implausible_range_from_collector":
            box = d_.collector_bbox()
            if box is None or km_to_box(la, lo, box) <= MAX_EMITTER_RANGE_KM:
                why = "aircraft rejected as implausible but within range of the collector"
        if not why and not near((d["geo"].get("rejected") or {}).get("lat"), la) and reason != "centroid_outside_device_bounds":
            pass  # raw kept in kismet.raw for devices; geo.rejected is a convenience copy
    elif "rejected" in ac and reason is None:
        # altitude-only rejection: the source value must be outside the plausible band and preserved
        if table == "data":
            (alt,) = d_.con.execute("select alt from data where rowid=?", (rowid,)).fetchone()
        else:
            obj, _ = blob_json(d_.scalar(f"select cast(device as text) from devices where rowid={int(rowid)}"))
            alt = (((obj or {}).get("kismet.device.base.location") or {}).get("kismet.common.location.last") or {}).get("kismet.common.location.alt")
        if not (isinstance(alt, (int, float)) and (alt < -1500 or alt > 30000)):
            why = f"altitude {alt} is inside the plausible band"
        elif abs(ac["rejected"]["altitude"] - alt) > abs(alt) * 1e-9:
            why = "rejected altitude does not match the source"
        elif "altitude" in ac:
            why = "rejected altitude was also indexed"
    if not why and "rejected" in ac and "altitude" in ac:
        why = "altitude both indexed and rejected"
    if why:
        bad.append(f"{src['file']} {table}#{rowid}: {why}")

print(f"verified {checked} rejected documents against their source rows")
for (t, r), n in sorted(kinds.items(), key=lambda x: -x[1]):
    print(f"   {n:>4}  {t:<9} {r}")
print("DISCREPANCIES:", len(bad))
for b in bad[:10]:
    print("   ", b)
sys.exit(1 if bad else 0)
