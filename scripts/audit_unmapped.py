#!/usr/bin/env python3
"""Report fields present in stored documents (_source) that have no mapping (so Kibana shows them as
"unmapped" and they cannot be searched, filtered or aggregated). Read-only; works with the ingest key.

  audit_unmapped.py [--per-record 1500] [--seed 7] [--json]

Fields inside the deliberately stored-only objects (enabled: false: kismet.raw, geo.rejected, aircraft.rejected,
kismet.ingest.tables/options) are expected to be unmapped and are listed separately.
Sampling is random per index and per kismet.record; a rare field can be missed, so run with a larger
--per-record for a stronger answer. Exit codes: 0 ok, 2 config error, 3 cannot connect.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import add_connection_args, load_config  # noqa: E402
from kismet_cartographer.constants import FAMILIES  # noqa: E402
from kismet_cartographer.esclient import ConnectionFailure, EsError, client_from_config  # noqa: E402


def mapped_paths(props, pre=""):
    """-> (mapped leaf/object paths, stored-only prefixes)"""
    mapped, opaque = set(), set()
    for k, v in props.items():
        path = pre + k
        mapped.add(path)
        if v.get("enabled") is False:
            opaque.add(path)
        if "properties" in v:
            m, o = mapped_paths(v["properties"], path + ".")
            mapped |= m
            opaque |= o
        for sub in (v.get("fields") or {}):
            mapped.add(f"{path}.{sub}")
    return mapped, opaque


def source_paths(obj, pre=""):
    """Yield (path, is_object) for every key in a document; arrays of objects are merged."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = pre + k
            if isinstance(v, dict):
                yield p, True
                yield from source_paths(v, p + ".")
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                yield p, True
                for x in v:
                    yield from source_paths(x, p + ".")
            else:
                yield p, False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-record", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    add_connection_args(ap)
    args = ap.parse_args()
    try:
        client = client_from_config(load_config(args=args), insecure=args.insecure)
        out = {}
        for idx in FAMILIES.values():
            mapping = client.get(f"/{idx}/_mapping")
            props = next(iter(mapping.values()))["mappings"].get("properties", {})
            mapped, opaque = mapped_paths(props)
            recs = client.post(f"/{idx}/_search", {"size": 0, "aggs": {"r": {"terms": {"field": "kismet.record", "size": 50}}}})
            counts = collections.defaultdict(lambda: collections.Counter())
            sampled = collections.Counter()
            for b in recs["aggregations"]["r"]["buckets"]:
                q = {"size": args.per_record, "query": {"function_score": {
                    "query": {"term": {"kismet.record": b["key"]}}, "random_score": {"seed": args.seed, "field": "_seq_no"}}}}
                for h in client.post(f"/{idx}/_search", q)["hits"]["hits"]:
                    sampled[b["key"]] += 1
                    for p, is_obj in source_paths(h["_source"]):
                        if p in mapped or any(p.startswith(o + ".") for o in opaque):
                            continue
                        if is_obj and any(m.startswith(p + ".") for m in mapped):
                            continue  # parent object of mapped fields
                        counts[b["key"]][p] += 1
            out[idx] = {"sampled": dict(sampled), "unmapped": {r: dict(c.most_common()) for r, c in counts.items()},
                        "stored_only_objects": sorted(opaque)}
    except ConnectionFailure as e:
        print(f"cannot connect: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"Elasticsearch error: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    for idx, r in out.items():
        print(f"\n== {idx}  sampled per record: {r['sampled']}")
        print(f"   stored-only (expected): {', '.join(r['stored_only_objects']) or '-'}")
        if not r["unmapped"]:
            print("   no unmapped fields found")
        for rec, fields in r["unmapped"].items():
            n = r["sampled"][rec]
            print(f"   record={rec} ({n} docs):")
            for f, c in fields.items():
                print(f"     {f:60s} {c:6d} ({100 * c / n:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
