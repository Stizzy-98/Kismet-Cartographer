#!/usr/bin/env python3
"""Create / update the Kibana layer of this project (idempotent).

Resolves the data view (reusing any existing one whose title is `kismet-cartographer-*`,
whatever its id; only creating `kismet-cartographer-dataview` when none exists) and then
upserts every saved object defined under config/kibana/saved-objects/:

  map        kismet-cartographer-map               Kismet Cartographer - Map
  search     kismet-cartographer-recent-observations
  lens       kismet-cartographer-lens-*            the dashboard's panels
  dashboard  kismet-cartographer-overview          Kismet Cartographer - Overview

Safety: every saved-object id must start with `kismet-cartographer-`; anything else is
refused before a single write happens, so an unrelated dashboard can never be overwritten.
Objects whose stored attributes/references already match the definition are left untouched,
which is what makes a second run a no-op.

The definition files are ordinary JSON. Kibana stores some attributes as JSON *strings*
(layerListJSON, panelsJSON, searchSourceJSON, ...); in the files these are written as real
JSON for readability and this installer serialises any key ending in `JSON` on the way in.
References to the data view use the placeholder id `kismet-cartographer-dataview` and are
rewritten to the id of the data view actually resolved.

Objects are written one at a time with `POST /api/saved_objects/<type>/<id>?overwrite=true`,
in dependency order (search, lens, map, dashboard). The bulk `_import` route is not used:
on Kibana 9.4 it replays the whole Lens migration chain over hand-written objects (they carry
no typeMigrationVersion) and answers HTTP 500; the per-object route stamps the current model
version itself.

Exit codes: 0 ok, 1 failure, 2 usage/config, 3 cannot connect/authenticate.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import add_connection_args, load_config  # noqa: E402
from kismet_cartographer.constants import (  # noqa: E402
    DATA_VIEW_FALLBACK_ID, DATA_VIEW_NAME, DATA_VIEW_TITLE, PREFIX)
from kismet_cartographer.esclient import (  # noqa: E402
    ConnectionFailure, EsError, kibana_client_from_config, kibana_space_path, require_owned)

CFG_DIR = ROOT / "config" / "kibana"
OBJ_DIR = CFG_DIR / "saved-objects"
# objects are imported in this order so that references always exist first
TYPE_ORDER = ["search", "lens", "map", "dashboard"]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stringify_json_attrs(value):
    """Kibana stores `*JSON` attributes as strings; the definition files keep them readable."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = stringify_json_attrs(v)
            if k.endswith("JSON") and not isinstance(v, str):
                v = json.dumps(v)
            out[k] = v
        return out
    if isinstance(value, list):
        return [stringify_json_attrs(v) for v in value]
    return value


def rewrite_refs(refs: list, data_view_id: str) -> list:
    out = []
    for r in refs:
        r = dict(r)
        if r.get("type") == "index-pattern" and r.get("id") == DATA_VIEW_FALLBACK_ID:
            r["id"] = data_view_id
        out.append(r)
    return out


def same(desired, stored) -> bool:
    """Compare only the keys we manage; Kibana adds defaults of its own on write."""
    if isinstance(desired, dict) and isinstance(stored, dict):
        return all(k in stored and same(v, stored[k]) for k, v in desired.items())
    if isinstance(desired, str) and isinstance(stored, str) and desired.startswith(("{", "[")):
        try:
            return same(json.loads(desired), json.loads(stored))
        except ValueError:
            return desired == stored
    if isinstance(desired, list) and isinstance(stored, list):
        return len(desired) == len(stored) and all(same(a, b) for a, b in zip(desired, stored))
    return desired == stored


def refresh_fields(client, dv_id: str, name: str, dry_run: bool) -> None:
    """Re-read the field list from Elasticsearch.

    A data view created while only a stub index existed keeps that stub's field list, so the
    panels and controls for fields added later (kismet.datasource.name, wifi.band,
    aircraft.icao, ...) would fail. The update is a no-op for an already current data view;
    `name` is sent back unchanged so nothing is renamed.
    """
    if dry_run:
        print(f"  [dry-run] POST /api/data_views/data_view/{dv_id} (refresh_fields)")
        return
    body = {"data_view": {"name": name}, "refresh_fields": True}
    client.post(f"/api/data_views/data_view/{dv_id}", body)


def resolve_data_view(client, dry_run: bool) -> tuple:
    """(id, name, note). Reuse any data view titled kismet-cartographer-*; create one only if none."""
    found = client.get("/api/data_views")
    for dv in found.get("data_view", []):
        if dv.get("title") == DATA_VIEW_TITLE:
            return (dv["id"], dv.get("name") or DATA_VIEW_NAME,
                    f"reused existing data view (title {DATA_VIEW_TITLE}, name {dv.get('name')!r})")
    spec = load(CFG_DIR / "data-view.json")["data_view"]
    spec["id"] = DATA_VIEW_FALLBACK_ID
    require_owned(spec["id"])
    if dry_run:
        return (DATA_VIEW_FALLBACK_ID, spec.get("name") or DATA_VIEW_NAME,
                "would be created (no data view titled kismet-cartographer-* exists)")
    made = client.post("/api/data_views/data_view", {"data_view": spec, "override": False})
    return made["data_view"]["id"], made["data_view"].get("name") or DATA_VIEW_NAME, "created"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print what would be done, change nothing")
    ap.add_argument("--force", action="store_true", help="re-import every object even if it is up to date")
    add_connection_args(ap, kibana=True)
    args = ap.parse_args()

    cfg = load_config(args=args)
    if not cfg.kibana_url:
        print("config error: KIBANA_URL is not set", file=sys.stderr)
        return 2
    try:
        client = kibana_client_from_config(cfg, insecure=args.insecure)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not OBJ_DIR.is_dir():
        print(f"config error: {OBJ_DIR} is missing", file=sys.stderr)
        return 2

    try:
        status = client.get("/api/status")
    except ConnectionFailure as e:
        print(f"cannot connect to {cfg.kibana_url}: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"authentication/authorisation failed: {e}", file=sys.stderr)
        return 3
    ver = status["version"]["number"]
    print(f"Kibana {ver} at {cfg.kibana_url}  ({status['status']['overall']['level']})")
    if int(ver.split(".")[0]) < 8:
        print("Kibana >= 8 is required.", file=sys.stderr)
        return 2

    # ---------------------------------------------------------------- definitions
    objects = []
    for f in sorted(OBJ_DIR.glob("*.json")):
        o = load(f)
        for key in ("id", "type", "attributes"):
            if key not in o:
                print(f"config error: {f} has no '{key}'", file=sys.stderr)
                return 2
        try:
            require_owned(o["id"])
        except ValueError:
            print(f"refused: {f.name} declares id '{o['id']}', which is not under the "
                  f"'{PREFIX}' namespace", file=sys.stderr)
            return 1
        if o["type"] not in TYPE_ORDER:
            print(f"config error: {f.name} has unsupported type '{o['type']}'", file=sys.stderr)
            return 2
        objects.append(o)
    if not objects:
        print(f"config error: no saved-object definitions in {OBJ_DIR}", file=sys.stderr)
        return 2

    summary = []
    try:
        data_view_id, data_view_name, note = resolve_data_view(client, args.dry_run)
        print(f"data view {data_view_id}: {note}")
        refresh_fields(client, data_view_id, data_view_name, args.dry_run)
        summary.append(f"data-view: {data_view_id}  ({data_view_name} / {DATA_VIEW_TITLE}) - "
                       f"{note}, field list refreshed")

        written, skipped = 0, 0
        for o in sorted(objects, key=lambda x: (TYPE_ORDER.index(x["type"]), x["id"])):
            body = {"id": o["id"], "type": o["type"],
                    "attributes": stringify_json_attrs(o["attributes"]),
                    "references": rewrite_refs(o.get("references", []), data_view_id)}
            title = body["attributes"].get("title", "")
            try:
                stored = client.get(f"/api/saved_objects/{o['type']}/{o['id']}")
            except EsError as e:
                if e.status != 404:
                    raise
                stored = None
            if stored is None:
                state = "create"
            elif args.force or not (same(body["attributes"], stored.get("attributes", {}))
                                    and same(body["references"], stored.get("references", []))):
                state = "update"
            else:
                state = "unchanged"
            summary.append(f"{o['type']}: {o['id']}  ({title}) - {state}")
            if state == "unchanged":
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [dry-run] POST /api/saved_objects/{o['type']}/{o['id']}?overwrite=true")
            else:
                client.post(f"/api/saved_objects/{o['type']}/{o['id']}", params={"overwrite": True},
                            body={"attributes": body["attributes"], "references": body["references"]})
            written += 1
        print(f"{written} object(s) {'would be ' if args.dry_run else ''}written, "
              f"{skipped} already up to date")
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except ConnectionFailure as e:
        print(f"connection lost: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"Kibana error: {e}", file=sys.stderr)
        return 3 if e.status in (401, 403) else 1

    print("\nObjects " + ("that would be" if args.dry_run else "created/updated") + ":")
    for s in summary:
        print("  -", s)
    base = cfg.kibana_url + kibana_space_path(cfg.space)
    print("\nOpen in Kibana:")
    print(f"  Overview (map + counts)   {base}/app/dashboards#/view/kismet-cartographer-overview")
    print(f"  Devices & Hardware        {base}/app/dashboards#/view/kismet-cartographer-hardware")
    print(f"  Map                       {base}/app/maps/map/kismet-cartographer-map")
    return 0


if __name__ == "__main__":
    sys.exit(main())
