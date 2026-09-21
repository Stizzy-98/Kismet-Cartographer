#!/usr/bin/env python3
"""Create the least-privilege INGEST API key and store it in a protected env file.

The key can only create/index/read `kismet-cartographer-*` (no cluster privileges, no Kibana, no
templates/pipelines, no deleting indices). The secret is written straight to a mode-600 file and is
never printed. Elasticsearch only lets a *user* (not another API key) create API keys, so this needs
ELASTIC_USERNAME/ELASTIC_PASSWORD (a user with `manage_own_api_key`).

  create_ingest_key.py                       # 180 day key -> ~/.config/kismet-cartographer/credentials.env
  create_ingest_key.py --expiry 90d --rotate # replace the key stored in that file

Exit codes: 0 ok, 1 failure, 2 config error, 3 cannot connect/authenticate.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer.config import DEFAULT_USER_ENV, load_config, parse_env_file  # noqa: E402
from kismet_cartographer.esclient import Client, ConnectionFailure, EsError  # noqa: E402

ROLE = {"cluster": [],
        "indices": [{"names": ["kismet-cartographer-*"],
                     "privileges": ["create_index", "create_doc", "index", "write", "read", "view_index_metadata"]}]}


def write_env(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700) if path.parent == DEFAULT_USER_ENV.parent else None
    lines = ["# Kismet Cartographer - ingest credentials. Mode 600. NEVER commit this file."]
    lines += [f"{k}={v}" for k, v in values.items() if v]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 600 from the first byte
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", help="installer credentials file (ELASTIC_USERNAME/ELASTIC_PASSWORD)")
    ap.add_argument("--out", default=str(DEFAULT_USER_ENV), help=f"where to store the ingest credentials (default {DEFAULT_USER_ENV})")
    ap.add_argument("--name", default="kismet-cartographer-ingest")
    ap.add_argument("--expiry", default="180d", help="e.g. 90d, 180d, 365d (default 180d; 'never' is refused)")
    ap.add_argument("--rotate", action="store_true", help="replace an ELASTIC_API_KEY already stored in --out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if args.expiry.lower() in ("never", "none", ""):
        print("refusing to create a non-expiring key; choose e.g. --expiry 180d", file=sys.stderr)
        return 2
    cfg = load_config(args.env_file)
    if not (cfg.username and cfg.password):
        print("config error: creating an API key needs ELASTIC_USERNAME and ELASTIC_PASSWORD "
              "(Elasticsearch does not allow an API key to create another API key).", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser()
    existing = parse_env_file(out) if out.is_file() else {}
    if existing.get("ELASTIC_API_KEY") and not args.rotate:
        print(f"{out} already contains an ELASTIC_API_KEY; use --rotate to replace it.", file=sys.stderr)
        return 2
    client = Client(cfg.elastic_url, username=cfg.username, password=cfg.password, ca_cert=cfg.ca_cert,
                    verify=cfg.verify_certs and not args.insecure)
    if args.dry_run:
        print(f"[dry-run] would create API key '{args.name}' (expires in {args.expiry}) and write it to {out}")
        return 0
    try:
        r = client.post("/_security/api_key", {
            "name": args.name, "expiration": args.expiry, "role_descriptors": {args.name: ROLE},
            "metadata": {"purpose": "KismetDB ingestion: create/index/read kismet-cartographer-* only",
                         "created_by": "scripts/create_ingest_key.py"}})
    except ConnectionFailure as e:
        print(f"cannot connect: {e}", file=sys.stderr)
        return 3
    except EsError as e:
        print(f"could not create the key: {e}", file=sys.stderr)
        return 3 if e.status in (401, 403) else 1
    values = {
        "ELASTIC_URL": cfg.elastic_url, "ELASTIC_CA_CERT": cfg.ca_cert, "ELASTIC_API_KEY": r["encoded"],
        "KIBANA_URL": cfg.kibana_url, "KIBANA_CA_CERT": cfg.kibana_ca_cert,
    }
    write_env(out, {**existing, **{k: v for k, v in values.items() if v}})
    import datetime
    exp = datetime.datetime.fromtimestamp(r["expiration"] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if r.get("expiration") else "?"
    print(f"created API key '{args.name}' (id {r['id']}, expires {exp}); secret stored in {out} (mode 600)")
    if existing.get("ELASTIC_API_KEY"):
        print("the previous key is still valid - invalidate it: DELETE /_security/api_key  {\"ids\":[\"<old id>\"]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
