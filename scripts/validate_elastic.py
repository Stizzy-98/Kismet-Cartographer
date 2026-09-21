#!/usr/bin/env python3
"""Validate what is in Elasticsearch: mapping, geospatial correctness, timestamps, completeness,
and (with --source) accuracy against the original KismetDB rows.

  validate_elastic.py                                  # checks that need only the index data
  validate_elastic.py --source wardrives/ --sample 300 # also compare sampled docs to the .kismet files
  validate_elastic.py --json > validation.json

Read-only; works with the write/read ingest key (checks that need cluster privileges are reported
as SKIP, not FAIL). Exit codes: 0 all PASS (WARN/SKIP allowed), 1 at least one FAIL, 2 config error,
3 cannot connect/authenticate.
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import add_connection_args, load_config  # noqa: E402
from kismet_cartographer.constants import FAMILIES, INDEX_DEVICES, INDEX_OBSERVATIONS, READ_ALIAS_ALL, PREFIX  # noqa: E402
from kismet_cartographer.esclient import ConnectionFailure, EsError, client_from_config  # noqa: E402
from kismet_cartographer.kismetdb import KismetDb, file_sha256, find_kismet_files  # noqa: E402
from kismet_cartographer.normalize import FileContext  # noqa: E402

ALL = READ_ALIAS_ALL
results = []
REFRESH_OK = False
REFRESH_WAIT = 35  # seconds: index.refresh_interval (30s) + margin


def record(name: str, status: str, detail: str = "") -> None:
    results.append({"check": name, "status": status, "detail": detail})
    if not ARGS.json:
        mark = {"PASS": "  PASS", "FAIL": "**FAIL", "WARN": "  WARN", "SKIP": "  SKIP"}[status]
        print(f"{mark}  {name}" + (f"  - {detail}" if detail else ""))


def search(es, body, index=ALL):
    return es.post(f"/{index}/_search", body)


def count(es, query=None, index=ALL) -> int:
    return es.post(f"/{index}/_count", {"query": query} if query else {})["count"]


# ------------------------------------------------------------------------------------ checks
def check_resources(es):
    try:
        r = es.get(f"/_alias/{PREFIX}*")
    except EsError as e:
        record("aliases exist", "FAIL", str(e))
        return
    aliases = {a for body in r.values() for a in (body.get("aliases") or {})}
    missing = [a for a in list(FAMILIES) + [ALL] if a not in aliases]
    record("write/read aliases exist", "FAIL" if missing else "PASS",
           f"missing: {missing}" if missing else f"{len(FAMILIES)} write aliases + {ALL}")
    for cap, path in (("index templates", "/_index_template/kismet-cartographer-*"),
                      ("ingest pipeline", "/_ingest/pipeline/kismet-cartographer-normalize-v1")):
        try:
            es.get(path)
            record(f"{cap} present", "PASS")
        except EsError as e:
            record(f"{cap} present", "SKIP" if e.status == 403 else "FAIL",
                   "key lacks cluster privilege to read it" if e.status == 403 else str(e))


def check_mapping(es):
    caps = es.get(f"/{PREFIX}*/_field_caps", params={"fields": "geo.location,geo.peak_signal_location"})["fields"]
    for f in ("geo.location", "geo.peak_signal_location"):
        types = sorted((caps.get(f) or {}).keys())
        ok = types == ["geo_point"]
        record(f"{f} mapped as geo_point everywhere", "PASS" if ok else "FAIL", f"types seen: {types}")
    maps = es.get(f"/{ALL}/_mapping")
    bad = []
    for idx, body in maps.items():
        m = body["mappings"]
        if m.get("dynamic") not in (False, "false"):
            bad.append(f"{idx}: dynamic={m.get('dynamic')}")
        raw = ((m.get("properties") or {}).get("kismet") or {}).get("properties", {}).get("raw", {})
        if raw.get("enabled") not in (False, "false"):
            bad.append(f"{idx}: kismet.raw not disabled")
    record("critical fields are not dynamically mapped (dynamic=false, kismet.raw stored only)",
           "FAIL" if bad else "PASS", "; ".join(bad))


def check_geo(es):
    r = search(es, {"size": 0, "track_total_hits": True, "aggs": {
        "with_loc": {"filter": {"exists": {"field": "geo.location"}}},
        "loc_not_ok": {"filter": {"bool": {"filter": [{"exists": {"field": "geo.location"}}],
                                           "must_not": [{"term": {"geo.status": "ok"}}]}}},
        "ok_no_loc": {"filter": {"bool": {"filter": [{"term": {"geo.status": "ok"}}],
                                          "must_not": [{"exists": {"field": "geo.location"}}]}}},
        "null_island": {"filter": {"geo_distance": {"distance": "100km", "geo.location": {"lat": 0, "lon": 0}}}},
        "bounds": {"geo_bounds": {"field": "geo.location", "wrap_longitude": False}},
        "peak_bad": {"filter": {"geo_distance": {"distance": "100km", "geo.peak_signal_location": {"lat": 0, "lon": 0}}}},
        "no_fix": {"filter": {"terms": {"geo.status": ["no_fix", "invalid"]}}},
        "reasons": {"terms": {"field": "geo.reason", "size": 10}},
        "types": {"terms": {"field": "geo.location_type", "size": 10}},
    }})
    a = r["aggregations"]
    total = r["hits"]["total"]["value"]
    record("documents indexed", "PASS" if total else "FAIL", f"{total:,} in {ALL}")
    record("every doc with geo.location has geo.status=ok", "PASS" if a["loc_not_ok"]["doc_count"] == 0 else "FAIL",
           f"violations: {a['loc_not_ok']['doc_count']:,}")
    record("no doc claims geo.status=ok without geo.location", "PASS" if a["ok_no_loc"]["doc_count"] == 0 else "FAIL",
           f"violations: {a['ok_no_loc']['doc_count']:,}")
    record("no fake (0,0)/Null-Island points (nothing within 100 km of 0,0)",
           "PASS" if a["null_island"]["doc_count"] == 0 and a["peak_bad"]["doc_count"] == 0 else "FAIL",
           f"geo.location={a['null_island']['doc_count']:,} peak={a['peak_bad']['doc_count']:,}")
    b = (a["bounds"] or {}).get("bounds")
    if b:
        tl, br = b["top_left"], b["bottom_right"]
        ok = -90 <= br["lat"] <= tl["lat"] <= 90 and -180 <= tl["lon"] <= br["lon"] <= 180
        record("latitude/longitude valid; bounding box of all points",
               "PASS" if ok else "FAIL",
               f"lat {br['lat']:.4f}..{tl['lat']:.4f}, lon {tl['lon']:.4f}..{br['lon']:.4f}")
        if ARGS.expect_bbox:
            la0, la1, lo0, lo1 = ARGS.expect_bbox
            outside = {"bool": {"filter": [{"exists": {"field": "geo.location"}}], "must_not": [{"geo_bounding_box": {
                "geo.location": {"top_left": {"lat": la1, "lon": lo0}, "bottom_right": {"lat": la0, "lon": lo1}}}}]}}
            by_type = search(es, {"size": 3, "query": outside, "track_total_hits": True,
                                  "_source": ["kismet.record", "kismet.source.file", "kismet.source.rowid", "geo.location", "geo.location_type"],
                                  "aggs": {"t": {"terms": {"field": "geo.location_type", "size": 10}}}})
            n_out = by_type["hits"]["total"]["value"]
            kinds = {x["key"]: x["doc_count"] for x in by_type["aggregations"]["t"]["buckets"]}
            collector_out = sum(v for k, v in kinds.items() if k != "emitter_reported")
            examples = "; ".join(f"{h['_source']['kismet']['record']} {h['_source']['kismet']['source']['file']}#{h['_source']['kismet']['source']['rowid']} "
                                 f"@({h['_source']['geo']['location']['lat']:.3f},{h['_source']['geo']['location']['lon']:.3f})"
                                 for h in by_type["hits"]["hits"])
            if n_out == 0:
                record("all points inside the expected bounding box (coordinates not reversed)", "PASS", f"lat {la0}..{la1}, lon {lo0}..{lo1}")
            elif collector_out:
                record("collector positions inside the expected bounding box (coordinates not reversed)", "FAIL",
                       f"{collector_out:,} observer/centroid docs outside lat {la0}..{la1}, lon {lo0}..{lo1}: {examples}")
            else:
                record("collector positions inside the expected bounding box (coordinates not reversed)", "PASS", "0 outside")
                record("aircraft-reported positions inside the expected bounding box", "WARN",
                       f"{n_out:,} emitter_reported doc(s) outside (aircraft can be far away, or Kismet's ADS-B decoder produced an implausible fix): {examples}")
        else:
            record("coordinates not reversed (needs --expect-bbox or --source)", "SKIP", "reversed pairs stay in range; verified via --source sampling")
        c_lat, c_lon = (tl["lat"] + br["lat"]) / 2, (tl["lon"] + br["lon"]) / 2
        n = count(es, {"geo_distance": {"distance": "5000km", "geo.location": {"lat": c_lat, "lon": c_lon}}})
        record("documents are searchable by location (geo_distance)", "PASS" if n else "FAIL", f"{n:,} hits within 5000 km of the bbox centre")
    else:
        record("bounding box", "FAIL", "no documents have geo.location")
    record("docs without a GPS fix carry no coordinates",
           "PASS", f"{a['no_fix']['doc_count']:,} no_fix/invalid docs; reasons "
           + ", ".join(f"{x['key']}={x['doc_count']:,}" for x in a["reasons"]["buckets"]))
    record("position semantics present", "PASS" if a["types"]["buckets"] else "FAIL",
           ", ".join(f"{x['key']}={x['doc_count']:,}" for x in a["types"]["buckets"]))


def check_fields(es):
    r = search(es, {"size": 0, "aggs": {
        "rssi0": {"filter": {"term": {"wifi.rssi": 0}}},
        "ts_min": {"min": {"field": "@timestamp"}}, "ts_max": {"max": {"field": "@timestamp"}},
        "no_ts": {"filter": {"bool": {"must_not": [{"exists": {"field": "@timestamp"}}]}}},
        "future": {"filter": {"range": {"@timestamp": {"gt": "now+1d"}}}},
        "ancient": {"filter": {"range": {"@timestamp": {"lt": "2000-01-01"}}}},
        "no_file": {"filter": {"bool": {"must_not": [{"exists": {"field": "kismet.source.file"}}]}}},
        "files": {"cardinality": {"field": "kismet.source.file_hash"}},
        "parse_errors": {"filter": {"exists": {"field": "kismet.parse_error"}}},
    }})["aggregations"]
    record("Kismet's 0 dBm sentinel never becomes wifi.rssi", "PASS" if a_eq(r["rssi0"], 0) else "FAIL", f"{r['rssi0']['doc_count']:,} docs with rssi 0")
    bad_ts = r["future"]["doc_count"] + r["ancient"]["doc_count"]
    lo, hi = r["ts_min"].get("value_as_string"), r["ts_max"].get("value_as_string")
    record("timestamps valid (none before 2000 or in the future)", "PASS" if bad_ts == 0 else "FAIL", f"range {lo} .. {hi}; bad: {bad_ts:,}")
    record("documents without @timestamp", "PASS" if r["no_ts"]["doc_count"] == 0 else "WARN",
           f"{r['no_ts']['doc_count']:,} (source rows with an implausible time are kept, never given an invented one)")
    record("every doc carries provenance (kismet.source.file)", "PASS" if a_eq(r["no_file"], 0) else "FAIL", f"{r['files']['value']} distinct capture files")
    record("records flagged kismet.parse_error", "PASS" if r["parse_errors"]["doc_count"] == 0 else "WARN", f"{r['parse_errors']['doc_count']:,}")


def a_eq(agg, n):
    return agg["doc_count"] == n


def _ledger_once(es):
    r = search(es, {"size": 1000, "query": {"term": {"kismet.record": "ingest.run"}},
                    "_source": ["kismet.source.file", "kismet.source.file_hash", "kismet.ingest.status",
                                "kismet.ingest.schema_version", "kismet.ingest.tables", "kismet.ingest.docs_indexed"]})
    markers = [h["_source"]["kismet"] for h in r["hits"]["hits"]]
    per_hash = search(es, {"size": 0, "aggs": {"h": {"terms": {"field": "kismet.source.file_hash", "size": 2000}, "aggs": {
        "t": {"terms": {"field": "kismet.source.table", "size": 10}}}}}})["aggregations"]["h"]["buckets"]
    actual = {b["key"]: {t["key"]: t["doc_count"] for t in b["t"]["buckets"]} for b in per_hash}
    bad, incomplete = [], []
    for m in markers:
        h, name = m["source"]["file_hash"], m["source"]["file"]
        if m["ingest"].get("status") != "complete":
            incomplete.append(name)
        for t, st in (m["ingest"].get("tables") or {}).items():
            have = actual.get(h, {}).get(t, 0)
            if have != st.get("docs", 0):
                bad.append(f"{name}:{t} expected {st.get('docs', 0):,} found {have:,}")
    return markers, bad, incomplete


def check_ledger(es):
    """Per-file ledger: documents the ingester says it built vs what is actually in the index."""
    markers, bad, incomplete = _ledger_once(es)
    if not markers:
        record("ingest ledger", "WARN", "no ingest.run markers found")
        return []
    waited = ""
    if bad and not REFRESH_OK and not ARGS.no_wait:
        # Each index refreshes on its own 30 s timer and this key may not force a refresh, so documents
        # ingested seconds ago can be visible in one index and not yet in another. Wait one interval.
        if not ARGS.json:
            print("       (ledger mismatch right after ingestion; waiting one refresh interval and re-checking)")
        time.sleep(REFRESH_WAIT)
        markers, bad, incomplete = _ledger_once(es)
        waited = " (after waiting for index refresh)"
    record("per-file ledger: expected == indexed document counts", "FAIL" if bad else "PASS",
           f"{len(markers)} files{waited}; " + ("; ".join(bad[:5]) if bad else "all tables match"))
    record("every ingested file completed", "PASS" if not incomplete else "WARN", ", ".join(incomplete[:5]))
    return markers


def check_source(es, markers):
    src_files = {}
    for f in find_kismet_files(ARGS.source):
        src_files[file_sha256(f)] = f
    rng = random.Random(ARGS.seed)
    checked = mismatches = 0
    notes = []
    for m in markers:
        h = m["source"]["file_hash"]
        if h not in src_files:
            continue
        db = KismetDb(src_files[h])
        ctx = FileContext(db, h, "validate")
        try:
            c, mm, n = compare_file(es, db, ctx, rng)
        finally:
            db.close()
        checked += c
        mismatches += mm
        notes += n
    if not checked:
        record("source comparison", "SKIP", "no ingested file matched a .kismet under --source")
        return
    record("sampled documents match the original KismetDB rows (coords, time, signal, frequency)",
           "FAIL" if mismatches else "PASS", f"{checked:,} field comparisons, {mismatches} mismatches"
           + ("; " + "; ".join(notes[:5]) if notes else ""))


def compare_file(es, db, ctx, rng):
    """Independent re-derivation of the documented rules from raw SQL values, compared to the index."""
    checks = mism = 0
    notes = []
    n = ARGS.sample

    def fetch(index, pairs):
        ids = [{"_index": index, "_id": ctx.doc_id(t, k)} for t, k in pairs]
        r = es.post("/_mget", {"docs": ids})
        return r["docs"]

    def near(a, b, tol=1e-9):
        return a is not None and b is not None and abs(a - b) <= tol

    # --- packets
    mx = db.scalar("select max(rowid) from packets") if db.has("packets") else 0
    if mx:
        ids = rng.sample(range(1, mx + 1), min(n, mx))
        rows = {r[0]: r for r in db.con.execute(
            f"select rowid, ts_sec, ts_usec, lat, lon, signal, frequency, sourcemac from packets where rowid in ({','.join(map(str, ids))})")}
        docs = fetch(INDEX_OBSERVATIONS, [("packets", i) for i in rows])
        for d, (rid, r) in zip(docs, rows.items()):
            checks += 1
            if not d.get("found"):
                mism += 1
                notes.append(f"{db.name}: packet rowid {rid} missing")
                continue
            s = d["_source"]
            _, ts, us, lat, lon, sig, freq, src = r
            loc = (s.get("geo") or {}).get("location")
            if lat == 0 and lon == 0:
                ok = loc is None and s["geo"]["status"] == "no_fix"
            elif abs(lat) > 90 or abs(lon) > 180:
                ok = loc is None and s["geo"]["status"] == "invalid"
            else:
                ok = bool(loc) and near(loc["lat"], lat) and near(loc["lon"], lon)  # lat->lat, lon->lon (not swapped)
            ok = ok and s["@timestamp"].startswith(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
            ok = ok and ((s.get("wifi") or {}).get("rssi") == (None if sig in (0, None) else float(sig)))
            ok = ok and ((s.get("wifi") or {}).get("frequency") in (None, freq / 1000.0)) and s["kismet"]["source"]["rowid"] == rid
            ok = ok and (s.get("source") or {}).get("mac") == src.upper()
            if not ok:
                mism += 1
                notes.append(f"{db.name}: packet rowid {rid} differs")
    # --- ADS-B frames
    mx = db.scalar("select max(rowid) from data") if db.has("data") else 0
    if mx:
        ids = rng.sample(range(1, mx + 1), min(n, mx))
        rows = {r[0]: r for r in db.con.execute(
            f"select rowid, lat, lon, alt, phyname, type from data where rowid in ({','.join(map(str, ids))})")}
        docs = fetch(INDEX_OBSERVATIONS, [("data", i) for i in rows])
        for d, (rid, r) in zip(docs, rows.items()):
            checks += 1
            if not d.get("found"):
                mism += 1
                notes.append(f"{db.name}: data rowid {rid} missing")
                continue
            s = d["_source"]
            loc = (s.get("geo") or {}).get("location")
            _, lat, lon, alt, phy, dtype = r
            kind = "emitter_reported" if (phy == "ADSB" or dtype == "ADSB") else "unspecified"  # unknown types: meaning not asserted
            geo_ = s.get("geo") or {}
            rej = geo_.get("rejected") or {}
            if lat == 0 and lon == 0:
                ok = loc is None and geo_.get("status") == "no_fix"
            elif abs(lat) > 90 or abs(lon) > 180:  # impossible coordinate: rejected, but the source value is kept
                ok = loc is None and geo_.get("status") == "invalid" and near(rej.get("lat"), lat) and near(rej.get("lon"), lon)
            elif kind == "emitter_reported" and ctx.implausible_emitter(lat, lon):  # farther than ADS-B range from the collector
                ok = (loc is None and geo_.get("reason") == "implausible_range_from_collector"
                      and near(rej.get("lat"), lat) and near(rej.get("lon"), lon))
            else:
                ok = bool(loc) and near(loc["lat"], lat) and near(loc["lon"], lon) and geo_.get("location_type") == kind
            # an absurd altitude must not be indexed as one (it is kept in aircraft.rejected)
            ac_alt = (s.get("aircraft") or {}).get("altitude")
            ok = ok and (ac_alt is None or -1500 <= ac_alt <= 30000)
            if not ok:
                mism += 1
                notes.append(f"{db.name}: adsb frame rowid {rid} differs")
    # --- devices (position via min/max box consistency, identity)
    rows = db.con.execute("select devkey, devmac, first_time, last_time, min_lat, min_lon, max_lat, max_lon from devices "
                          "order by random() limit ?", (min(n, 200),)).fetchall() if db.has("devices") else []
    if rows:
        docs = fetch(INDEX_DEVICES, [("devices", r[0]) for r in rows])
        for d, r in zip(docs, rows):
            checks += 1
            devkey, devmac, ft, lt, mnl, mnn, mxl, mxn = r
            if not d.get("found"):
                mism += 1
                notes.append(f"{db.name}: device {devkey} missing")
                continue
            s = d["_source"]
            ok = s["kismet"]["devmac"] == devmac.upper() and s["kismet"]["device_key"] == devkey
            loc = (s.get("geo") or {}).get("location")
            if loc and mnl and mxl:  # a real position must sit inside the device's own min/max box
                ok = ok and (min(mnl, mxl) - 1e-3 <= loc["lat"] <= max(mnl, mxl) + 1e-3) and (min(mnn, mxn) - 1e-3 <= loc["lon"] <= max(mnn, mxn) + 1e-3)
            if not ok:
                mism += 1
                notes.append(f"{db.name}: device {devkey} differs")
    return checks, mism, notes


def main() -> int:
    global ARGS, REFRESH_OK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help=".kismet file/dir to compare sampled documents against")
    ap.add_argument("--sample", type=int, default=200, help="documents sampled per table per file (default 200)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--expect-bbox", type=float, nargs=4, metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for index refresh before re-checking the ledger")
    add_connection_args(ap)
    ARGS = ap.parse_args()
    cfg = load_config(args=ARGS)
    try:
        es = client_from_config(cfg, insecure=ARGS.insecure)
        es.get(f"/_alias/{PREFIX}*")
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (ConnectionFailure, EsError) as e:
        print(f"cannot connect/authenticate: {e}", file=sys.stderr)
        return 3
    try:
        es.post(f"/{PREFIX}*/_refresh")
        REFRESH_OK = True
    except EsError:
        REFRESH_OK = False  # needs the `maintenance` privilege; the ledger check compensates
    if not ARGS.json:
        print(f"Validating {cfg.elastic_url} ({ALL})\n")
    check_resources(es)
    check_mapping(es)
    check_geo(es)
    check_fields(es)
    markers = check_ledger(es)
    if ARGS.source and markers:
        check_source(es, markers)
    elif ARGS.source:
        record("source comparison", "SKIP", "no ingest markers")
    fails = sum(1 for r in results if r["status"] == "FAIL")
    if ARGS.json:
        json.dump({"failures": fails, "results": results}, sys.stdout, indent=2)
        print()
    else:
        print(f"\n{len(results)} checks, {fails} failed, {sum(1 for r in results if r['status'] == 'WARN')} warnings")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
