#!/usr/bin/env python3
"""Create / update the Elasticsearch resources of this project (idempotent).

Creates, all under the `kismet-cartographer-` prefix:
  component templates  kismet-cartographer-settings-v1, kismet-cartographer-mappings-common-v1
  ingest pipeline      kismet-cartographer-normalize-v1
  index templates      kismet-cartographer-{observations,devices,track,system}
  indices + aliases    kismet-cartographer-<family>-v1  with write alias kismet-cartographer-<family>
                       and read alias kismet-cartographer-all

Safety: every name is checked against the prefix, and an existing resource that is not tagged
`_meta.project = kismet-cartographer` is never overwritten. Needs the API key from
docs/installation.md (cluster: manage_index_templates + manage_ingest_pipelines; indices: create_index + manage on
kismet-cartographer-*).

Exit codes: 0 ok, 1 failure, 2 usage/config, 3 cannot connect/authenticate.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import add_connection_args, load_config  # noqa: E402
from kismet_cartographer.constants import FAMILIES, PIPELINE_NORMALIZE, READ_ALIAS_ALL  # noqa: E402
from kismet_cartographer.esclient import ConnectionFailure, EsError, client_from_config, require_owned  # noqa: E402

CFG_DIR = ROOT / "config" / "elasticsearch"
OWNER = "kismet-cartographer"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def owned_or_absent(client, kind_path: str, name: str, list_key: str = "") -> bool:
    """True if the resource does not exist yet, or exists and is ours. False = someone else's."""
    try:
        r = client.get(f"{kind_path}/{name}")
    except EsError as e:
        if e.status == 404:
            return True
        raise
    if list_key:  # component/index templates come back wrapped in a list
        items = r.get(list_key) or []
        body = (items[0].get("component_template") or items[0].get("index_template") or {}) if items else {}
    else:
        body = r.get(name, r)
    return (body.get("_meta") or {}).get("project") == OWNER


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replicas", type=int, default=None, help="number_of_replicas for new indices (default 0)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be done, change nothing")
    ap.add_argument("--no-indices", action="store_true", help="templates + pipeline only; do not create indices/aliases")
    add_connection_args(ap)
    args = ap.parse_args()

    cfg = load_config(args=args)
    try:
        client = client_from_config(cfg, installer=True, insecure=args.insecure)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        info = client.get("/")
    except ConnectionFailure as e:
        print(f"cannot connect to {cfg.elastic_url}: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"authentication/authorisation failed: {e}", file=sys.stderr)
        return 3
    ver = info["version"]["number"]
    print(f"Elasticsearch {ver} at {cfg.elastic_url}  (cluster {info.get('cluster_name')})")
    if int(ver.split(".")[0]) < 8:
        print("Elasticsearch >= 8 is required (composable templates, ECS 8).", file=sys.stderr)
        return 2

    created = []  # for the summary

    def put(kind: str, path: str, name: str, body: dict, list_key: str = "") -> None:
        require_owned(name)
        if not owned_or_absent(client, path, name, list_key):
            raise RuntimeError(f"{kind} '{name}' already exists and is not managed by this project; refusing to overwrite")
        if args.dry_run:
            print(f"  [dry-run] PUT {path}/{name}")
        else:
            client.put(f"{path}/{name}", body)
        created.append(f"{kind}: {name}")

    try:
        # 1. component templates
        for f in sorted((CFG_DIR / "component-templates").glob("*.json")):
            body = load(f)
            if args.replicas is not None and f.stem.endswith("settings-v1"):
                body["template"]["settings"]["index"]["number_of_replicas"] = args.replicas
            put("component template", "/_component_template", f.stem, body, "component_templates")
        # 2. ingest pipeline (must exist before the index templates that reference it are used)
        for f in sorted((CFG_DIR / "pipelines").glob("*.json")):
            put("ingest pipeline", "/_ingest/pipeline", f.stem, load(f))
        # 3. index templates
        for f in sorted((CFG_DIR / "index-templates").glob("*.json")):
            put("index template", "/_index_template", f.stem, load(f), "index_templates")

        # 4. indices + aliases
        if not args.no_indices:
            common = load(CFG_DIR / "component-templates" / "kismet-cartographer-mappings-common-v1.json")
            mappings = common["template"]["mappings"]
            for alias, concrete in FAMILIES.items():
                require_owned(concrete)
                require_owned(alias)
                if client.exists(f"/{concrete}"):
                    print(f"  index {concrete} exists - re-applying mappings + aliases")
                    if not args.dry_run:
                        try:
                            client.put(f"/{concrete}/_mapping", mappings)
                        except EsError as e:
                            print(f"  ! mapping update rejected for {concrete}: {e}\n"
                                  f"    a type change needs a reindex - see docs/operations.md", file=sys.stderr)
                            return 1
                        client.post("/_aliases", {"actions": [
                            {"add": {"index": concrete, "alias": alias, "is_write_index": True}},
                            {"add": {"index": concrete, "alias": READ_ALIAS_ALL}}]})
                else:
                    if args.dry_run:
                        print(f"  [dry-run] PUT /{concrete} (aliases {alias}, {READ_ALIAS_ALL})")
                    else:
                        client.put(f"/{concrete}", {"aliases": {alias: {"is_write_index": True}, READ_ALIAS_ALL: {}}})
                created.append(f"index: {concrete}  (write alias {alias}, read alias {READ_ALIAS_ALL})")

        # 5. self-test the pipeline: the guarantees documented in docs/data-model.md must hold.
        if not args.dry_run:
            sim = client.post(f"/_ingest/pipeline/{PIPELINE_NORMALIZE}/_simulate", {"docs": [
                {"_source": {"geo": {"location": {"lat": 0, "lon": 0}, "location_type": "observer", "status": "ok"}}},
                {"_source": {"geo": {"location": {"lat": 40.66, "lon": -100.24}, "location_type": "observer", "status": "ok"}}},
                {"_source": {"geo": {"location": {"lat": 140.66, "lon": -100.24}, "status": "ok"}}},  # latitude out of range
                {"_source": {"wifi": {"rssi": 0}}},
            ]})
            d = [x["doc"]["_source"] for x in sim["docs"]]
            ok = ("location" not in d[0]["geo"] and d[0]["geo"]["status"] == "invalid"
                  and d[1]["geo"]["location"] == {"lat": 40.66, "lon": -100.24}
                  and "location" not in d[2]["geo"] and "rssi" not in d[3]["wifi"]
                  and "ingested" in d[1]["event"])
            print("  pipeline self-test:", "PASS" if ok else "FAIL")
            if not ok:
                print(json.dumps(d, indent=1), file=sys.stderr)
                return 1
    except RuntimeError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except ConnectionFailure as e:
        print(f"connection lost: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"Elasticsearch error: {e}", file=sys.stderr)
        return 3 if e.status in (401, 403) else 1

    print("\nResources " + ("that would be" if args.dry_run else "created/updated") + ":")
    for c in created:
        print("  -", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
