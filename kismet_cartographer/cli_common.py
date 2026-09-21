"""Pieces shared by the two command-line steps, ./install and ./ingest."""
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
WARDRIVES = ROOT / "wardrives"
ENV_MARKER = "# Written by Kismet Cartographer (--save-config). Settings only: the API key itself is not stored here."


def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say(f"  [ ok ] {msg}")


def warn(msg):
    say(f"  [warn] {msg}")


class Stop(Exception):
    def __init__(self, code, msg, fix=""):
        self.code, self.msg, self.fix = code, msg, fix


def connection_hint(err: str, url: str = "") -> str:
    e = err.lower()
    if url.startswith("http://") and ("reset by peer" in e or "remote end closed" in e or "badstatusline" in e
                                      or "bad status line" in e or "eof occurred" in e):
        return ("The server hung up on a plain-HTTP request, which usually means it uses HTTPS. "
                "Use https:// in the URL (with --host: add --https) and pass the CA with --ca <file>.")
    if "certificate verify failed" in e and ("hostname" in e or "mismatch" in e or "not valid for" in e):
        return ("The certificate is trusted but is not valid for the name in your URL. Use the name the certificate was "
                "issued for in the URL, or keep your URL and add --tls-server-name <name> (Kibana: --kibana-tls-server-name). "
                "For `kubectl port-forward` on ECK the names are <cluster>-es-http.<namespace>.svc and "
                "<kibana>-kb-http.<namespace>.svc. See docs/installation.md.")
    if "certificate verify failed" in e or "self-signed" in e or "unable to get local issuer" in e:
        return ("The server's certificate is not signed by a CA this machine trusts. Pass the CA with --ca <file> "
                "(and --kibana-ca <file> if Kibana uses a different one). How to get it: docs/installation.md.")
    if "wrong version number" in e or "unknown protocol" in e:
        return "The server answered in plain HTTP. Use http:// in the URL, or point at the HTTPS port."
    if "refused" in e:
        return "Nothing is listening there. Check the host and port (is the port-forward / service running?)."
    if "name or service not known" in e or "nodename nor servname" in e or "getaddrinfo" in e:
        return "The host name does not resolve. Check the URL."
    if "no route to host" in e or "unreachable" in e:
        return "The host cannot be reached from this machine. Check the URL, VPN/firewall, or the port-forward."
    if "timed out" in e:
        return "The connection timed out. Check the URL, firewall, or that the port-forward is still running."
    return ""


def run_step(title, script, extra, env, dry):
    say(f"\n==> {title}")
    cmd = [sys.executable, str(ROOT / "scripts" / script)] + extra + (["--dry-run"] if dry else [])
    rc = subprocess.call(cmd, env=env)
    if rc:
        raise Stop(rc if rc in (2, 3) else 1, f"{title} failed (exit {rc}, see above)", "")


def capture_files(path) -> list:
    p = Path(path)
    return sorted(p.glob("*.kismet")) if p.is_dir() else ([p] if p.is_file() else [])


def child_env(cfg) -> dict:
    """Hand the settings to the helper scripts through the environment, so the key never appears in `ps`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ELASTIC_", "KIBANA_", "KISMET_CARTOGRAPHER"))}
    env.update({"ELASTIC_URL": cfg.elastic_url, "ELASTIC_API_KEY": cfg.api_key, "ELASTIC_INSTALL_API_KEY": cfg.api_key,
                "KIBANA_URL": cfg.kibana_url, "KIBANA_API_KEY": cfg.api_key,
                "ELASTIC_CA_CERT": cfg.ca_cert, "KIBANA_CA_CERT": cfg.kibana_ca_cert,
                "ELASTIC_VERIFY_CERTS": "true" if cfg.verify_certs else "false",
                "ELASTIC_TLS_SERVER_NAME": cfg.tls_server_name, "KIBANA_TLS_SERVER_NAME": cfg.kibana_tls_server_name,
                "KIBANA_SPACE": cfg.space})
    return env



def write_env_file(cfg, path: Path, key_file: str = "") -> List[str]:
    """Save the non-secret connection settings (never the API key, only the path of the file that holds it).

    Returns the names written. Refuses to overwrite a file this tool did not write.
    """
    path = Path(path)
    if path.exists() and ENV_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
        raise FileExistsError(f"{path} exists and was not written by this tool; pass another file to --save-config")
    def absolute(p):
        return str(Path(p).expanduser().resolve()) if p else ""
    items = [("ELASTIC_URL", cfg.elastic_url), ("KIBANA_URL", cfg.kibana_url),
             ("ELASTIC_CA_CERT", absolute(cfg.ca_cert)),
             ("KIBANA_CA_CERT", absolute(cfg.kibana_ca_cert) if cfg.kibana_ca_cert != cfg.ca_cert else ""),
             ("ELASTIC_TLS_SERVER_NAME", cfg.tls_server_name), ("KIBANA_TLS_SERVER_NAME", cfg.kibana_tls_server_name),
             ("KIBANA_SPACE", cfg.space), ("ELASTIC_API_KEY_FILE", absolute(key_file)),
             ("ELASTIC_VERIFY_CERTS", "" if cfg.verify_certs else "false")]
    written = [(k, v) for k, v in items if v]
    body = ENV_MARKER + "\n" + "".join(f"{k}={v}\n" for k, v in written)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return [k for k, _ in written]
