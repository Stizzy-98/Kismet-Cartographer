"""Credential/config loading. Secrets come from the environment or a protected file.

Precedence (highest first):
  1. command-line flags (--es-url, --kibana-url, --api-key, --api-key-file, --ca, ...)
  2. real environment variables
  3. --env-file / $KISMET_CARTOGRAPHER_ENV
  4. ./.env
  5. <repo>/.env
  6. ~/.config/kismet-cartographer/credentials.env

Nothing here ever prints a secret; Config.__repr__ redacts them.
"""
from __future__ import annotations

import argparse
import base64
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_ENV = Path.home() / ".config" / "kismet-cartographer" / "credentials.env"

_SECRET_KEYS = {"ELASTIC_API_KEY", "ELASTIC_INSTALL_API_KEY", "ELASTIC_PASSWORD", "KIBANA_API_KEY", "KIBANA_PASSWORD"}


def parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val and val[0] in "'\"":
            q = val[0]
            end = val.find(q, 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            val = val.split(" #", 1)[0].strip()  # strip trailing comment
        out[key.strip()] = val
    return out


def _warn_if_loose(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"WARNING: {path} is readable by other users; run: chmod 600 {path}", file=sys.stderr)


@dataclass
class Config:
    elastic_url: str = ""
    api_key: str = ""            # ingest/validate credential (ELASTIC_API_KEY)
    install_api_key: str = ""    # installer credential (ELASTIC_INSTALL_API_KEY)
    username: str = ""
    password: str = ""
    ca_cert: str = ""
    verify_certs: bool = True
    kibana_url: str = ""
    kibana_ca_cert: str = ""
    kibana_api_key: str = ""
    tls_server_name: str = ""    # Elasticsearch: name to verify in the certificate when the URL uses another host
    kibana_tls_server_name: str = ""  # same, for Kibana (kubectl port-forward: the two services have different names)
    space: str = ""              # Kibana space id ("" = default)
    sources: List[str] = field(default_factory=list)

    def __repr__(self) -> str:  # never leak secrets into logs/tracebacks
        return (f"Config(elastic_url={self.elastic_url!r}, kibana_url={self.kibana_url!r}, "
                f"auth={'api_key' if self.api_key else 'none'}, "
                f"installer={'api_key' if self.install_api_key else ('basic' if self.username else 'none')}, "
                f"verify_certs={self.verify_certs}, sources={self.sources})")


def load_config(env_file: Optional[str] = None, args: Optional[argparse.Namespace] = None) -> Config:
    if args is not None and env_file is None:
        env_file = getattr(args, "env_file", None)
    values: Dict[str, str] = {}
    used: List[str] = []
    candidates: List[Path] = [DEFAULT_USER_ENV, REPO_ROOT / ".env", Path.cwd() / ".env"]
    explicit = env_file or os.environ.get("KISMET_CARTOGRAPHER_ENV")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for p in candidates:  # later entries override earlier ones
        if p.is_file():
            _warn_if_loose(p)
            values.update(parse_env_file(p))
            used.append(str(p))
    for k, v in os.environ.items():  # real environment wins
        if k.startswith(("ELASTIC_", "KIBANA_")):
            values[k] = v

    def truthy(v: str, default: bool) -> bool:
        if v == "":
            return default
        return v.strip().lower() not in ("0", "false", "no", "off")

    cfg = Config(
        elastic_url=values.get("ELASTIC_URL", "").rstrip("/"),
        api_key=values.get("ELASTIC_API_KEY", ""),
        install_api_key=values.get("ELASTIC_INSTALL_API_KEY", ""),
        username=values.get("ELASTIC_USERNAME", ""),
        password=values.get("ELASTIC_PASSWORD", ""),
        ca_cert=values.get("ELASTIC_CA_CERT", ""),
        verify_certs=truthy(values.get("ELASTIC_VERIFY_CERTS", ""), True),
        kibana_url=values.get("KIBANA_URL", "").rstrip("/"),
        kibana_ca_cert=values.get("KIBANA_CA_CERT", "") or values.get("ELASTIC_CA_CERT", ""),
        kibana_api_key=values.get("KIBANA_API_KEY", ""),
        tls_server_name=values.get("ELASTIC_TLS_SERVER_NAME", ""),
        kibana_tls_server_name=values.get("KIBANA_TLS_SERVER_NAME", ""),
        space=values.get("KIBANA_SPACE", ""),
        sources=used,
    )
    key_file = values.get("ELASTIC_API_KEY_FILE", "")
    if key_file and not cfg.api_key:  # a saved setting points at the file that holds the key
        p = Path(key_file).expanduser()
        lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
        if lines and lines[0].strip():
            cfg.api_key = normalize_api_key(lines[0])
            _warn_if_loose(p)
        else:
            print(f"WARNING: ELASTIC_API_KEY_FILE points at {p}, which is missing or empty", file=sys.stderr)
    if args is not None:
        apply_args(cfg, args)
    return cfg


def normalize_api_key(key: str) -> str:
    """Accept the "encoded" value Elasticsearch/Kibana show, or a bare "id:api_key" pair."""
    key = key.strip()
    if key.lower().startswith("apikey "):
        key = key[7:].strip()
    if ":" in key:  # id:secret -> base64(id:secret)
        key = base64.b64encode(key.encode()).decode()
    return key


def add_connection_args(ap: argparse.ArgumentParser, *, kibana: bool = False) -> None:
    """The flags every script shares. One API key is used for everything."""
    g = ap.add_argument_group("connection (flags override environment variables and env files)")
    g.add_argument("--es-url", metavar="URL", help="Elasticsearch URL, e.g. https://localhost:9200")
    if kibana:
        g.add_argument("--kibana-url", metavar="URL", help="Kibana URL, e.g. https://localhost:5601 (include any base path)")
    g.add_argument("--host", metavar="IP_OR_NAME",
                   help="shortcut for a stack that runs on one machine: builds the URLs from this address "
                        "(http:// unless you give --ca or --https). --es-url / --kibana-url win if also given")
    g.add_argument("--https", action="store_true", help="with --host: use https:// (implied by --ca)")
    g.add_argument("--es-port", type=int, default=9200, metavar="PORT", help="with --host: Elasticsearch port (default 9200)")
    if kibana:
        g.add_argument("--kibana-port", type=int, default=5601, metavar="PORT", help="with --host: Kibana port (default 5601)")
    g.add_argument("--api-key", metavar="KEY",
                   help="API key (the 'encoded' value, or id:secret). Visible in `ps` and shell history - "
                        "prefer --api-key-file or the ELASTIC_API_KEY environment variable")
    g.add_argument("--api-key-file", metavar="FILE", help="read the API key from FILE (first line)")
    g.add_argument("--ca", metavar="FILE", help="PEM CA certificate that signed the Elasticsearch certificate "
                                                "(omit if it is signed by a public CA)")
    if kibana:
        g.add_argument("--kibana-ca", metavar="FILE", help="PEM CA for Kibana (default: same as --ca)")
        g.add_argument("--space", metavar="ID", help="Kibana space to install into (default: the default space)")
    g.add_argument("--tls-server-name", metavar="NAME",
                   help="Elasticsearch: hostname to verify in the certificate when you connect through another name, "
                        "e.g. kubectl port-forward to localhost")
    if kibana:
        g.add_argument("--kibana-tls-server-name", metavar="NAME", help="the same, for Kibana")
    g.add_argument("--insecure", action="store_true", help="do NOT verify TLS certificates (throw-away tests only)")
    g.add_argument("--env-file", metavar="FILE", help="KEY=value file with the same settings (mode 600)")


def apply_args(cfg: Config, args: argparse.Namespace) -> None:
    """Flags win over everything else. One key serves Elasticsearch and Kibana."""
    g = lambda n: getattr(args, n, None)  # noqa: E731  (flags a script does not define are simply absent)
    if g("es_url"):
        cfg.elastic_url = g("es_url").rstrip("/")
    if g("kibana_url"):
        cfg.kibana_url = g("kibana_url").rstrip("/")
    key = ""
    if g("api_key_file"):
        p = Path(g("api_key_file")).expanduser()
        lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
        key = lines[0] if lines else ""
        if not key.strip():
            raise SystemExit(f"error: --api-key-file {p} is missing or empty")
        _warn_if_loose(p)
    elif g("api_key"):
        key = g("api_key")
    if key:
        key = normalize_api_key(key)
        cfg.api_key = cfg.install_api_key = cfg.kibana_api_key = key
    elif cfg.api_key:
        cfg.api_key = normalize_api_key(cfg.api_key)
    if g("ca"):
        cfg.ca_cert = g("ca")
        if not g("kibana_ca"):
            cfg.kibana_ca_cert = cfg.kibana_ca_cert if os.environ.get("KIBANA_CA_CERT") else g("ca")
    if g("kibana_ca"):
        cfg.kibana_ca_cert = g("kibana_ca")
    if g("tls_server_name"):
        cfg.tls_server_name = g("tls_server_name")
    if g("kibana_tls_server_name"):
        cfg.kibana_tls_server_name = g("kibana_tls_server_name")
    if g("space"):
        cfg.space = g("space")
    if g("insecure"):
        cfg.verify_certs = False
    if g("host"):
        host = g("host").strip()
        if ":" in host and not host.startswith("["):  # bare IPv6 address
            host = f"[{host}]"
        scheme = "https" if (g("https") or g("ca")) else "http"
        if not g("es_url"):
            cfg.elastic_url = f"{scheme}://{host}:{g('es_port') or 9200}"
        if not g("kibana_url") and hasattr(args, "kibana_port"):
            cfg.kibana_url = f"{scheme}://{host}:{g('kibana_port') or 5601}"
    for label, path in (("--ca", cfg.ca_cert), ("--kibana-ca", cfg.kibana_ca_cert)):
        if path and not Path(path).expanduser().is_file():
            raise SystemExit(f"error: {label} file not found: {path}")
