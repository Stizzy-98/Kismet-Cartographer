"""Minimal Elasticsearch/Kibana HTTP client (stdlib only).

* TLS is verified against the CA file you configure (ECK uses a private CA). Verification
  is only disabled when ELASTIC_VERIFY_CERTS=false / --insecure is given explicitly.
* Retries with exponential backoff on connection errors and 429/502/503/504.
* `require_owned()` is the safety rail for the cluster-wide template/pipeline privileges:
  every mutating call on a named resource must be under the `kismet-cartographer-` prefix.
"""
from __future__ import annotations

import base64
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .constants import PREFIX

RETRY_STATUS = {429, 502, 503, 504}


class EsError(Exception):
    def __init__(self, status: int, body: Any, method: str = "", path: str = ""):
        self.status, self.body, self.method, self.path = status, body, method, path
        reason = body
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            reason = err.get("reason") if isinstance(err, dict) else err
            if isinstance(err, dict) and err.get("root_cause"):
                reason = f"{reason} [{err['root_cause'][0].get('reason')}]"
        super().__init__(f"{method} {path} -> HTTP {status}: {reason}")


class ConnectionFailure(Exception):
    """Could not reach the server at all (DNS, TLS verification, refused, timeout)."""


def require_owned(name: str) -> str:
    """Refuse to mutate any named resource outside this project's namespace."""
    bare = name.lstrip("/").split("/")[0].split(",")[0]
    if not bare.startswith(PREFIX):
        raise ValueError(f"refusing to touch '{name}': not under the '{PREFIX}' namespace")
    return name


def make_ssl_context(ca_cert: str = "", verify: bool = True) -> ssl.SSLContext:
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=ca_cert) if ca_cert else ssl.create_default_context()


class _NamedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that checks the certificate against `server_name` instead of the URL's host.

    Needed for `kubectl port-forward` (the URL says localhost, the certificate says
    <cluster>-es-http.<namespace>.svc) without turning verification off.
    """
    server_name = ""

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_name or self.host)


class _NamedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ctx: ssl.SSLContext, server_name: str):
        super().__init__(context=ctx)
        self._ctx, self._name = ctx, server_name

    def https_open(self, req):
        name = self._name

        def make(host, **kw):
            conn = _NamedHTTPSConnection(host, **kw)
            conn.server_name = name
            return conn
        return self.do_open(make, req, context=self._ctx)


class Client:
    def __init__(self, base_url: str, *, api_key: str = "", username: str = "", password: str = "",
                 ca_cert: str = "", verify: bool = True, timeout: float = 120.0,
                 extra_headers: Optional[Dict[str, str]] = None, max_retries: int = 6, server_name: str = ""):
        if not base_url:
            raise ValueError("no URL configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.ctx = make_ssl_context(ca_cert, verify)
        self.opener = (urllib.request.build_opener(_NamedHTTPSHandler(self.ctx, server_name))
                       if server_name and verify else None)
        self.headers = {"Accept": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif username:
            tok = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {tok}"
        self.headers.update(extra_headers or {})

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None,
                ok: Tuple[int, ...] = (200, 201), raw_body: Optional[bytes] = None,
                content_type: str = "application/json", expect_json: bool = True) -> Any:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()})
        data = raw_body
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = content_type
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                opened = (self.opener.open(req, timeout=self.timeout) if self.opener
                          else urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx))
                with opened as resp:
                    payload, status = resp.read(), resp.status
            except urllib.error.HTTPError as e:
                payload, status = e.read(), e.code
                if status in RETRY_STATUS and attempt <= self.max_retries:
                    time.sleep(min(30.0, 0.5 * 2 ** attempt))
                    continue
            except (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError, OSError) as e:
                if attempt <= self.max_retries and not _is_tls_verify_error(e):
                    time.sleep(min(30.0, 0.5 * 2 ** attempt))
                    continue
                raise ConnectionFailure(f"{method} {url}: {getattr(e, 'reason', e)}") from e
            parsed: Any = payload
            if payload and expect_json:
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    parsed = payload.decode("utf-8", "replace")
            elif not payload:
                parsed = {}
            if status in ok:
                return parsed
            raise EsError(status, parsed, method, path)

    def get(self, path, **kw): return self.request("GET", path, **kw)
    def put(self, path, body=None, **kw): return self.request("PUT", path, body, **kw)
    def post(self, path, body=None, **kw): return self.request("POST", path, body, **kw)
    def delete(self, path, **kw): return self.request("DELETE", path, **kw)

    def exists(self, path: str) -> bool:
        try:
            self.request("HEAD", path, ok=(200,), expect_json=False)
            return True
        except EsError as e:
            if e.status == 404:
                return False
            raise

    # --------------------------------------------------------------- Elastic
    def bulk(self, ndjson: bytes) -> Dict[str, Any]:
        return self.request(
            "POST", "/_bulk", raw_body=ndjson, content_type="application/x-ndjson",
            params={"filter_path": "errors,items.*.status,items.*.error,items.*._id,items.*._index"})


def _is_tls_verify_error(e: BaseException) -> bool:
    text = str(getattr(e, "reason", e))
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text or "hostname" in text.lower() and "match" in text.lower()


def client_from_config(cfg, *, installer: bool = False, insecure: bool = False, timeout: float = 120.0) -> Client:
    """Build an Elasticsearch client. `installer=True` prefers the installer credential."""
    verify = cfg.verify_certs and not insecure
    if installer:
        key, user, pw = cfg.install_api_key, cfg.username, cfg.password
        if not (key or user):
            key = cfg.api_key  # fall back to the normal key if it has the privileges
    else:
        key, user, pw = cfg.api_key, "", ""
        if not key:
            key, user, pw = cfg.install_api_key, cfg.username, cfg.password
    if not (key or user):
        raise ValueError("no Elasticsearch credential configured (ELASTIC_API_KEY, or ELASTIC_USERNAME/ELASTIC_PASSWORD)")
    return Client(cfg.elastic_url, api_key=key, username=user, password=pw,
                  ca_cert=cfg.ca_cert, verify=verify, timeout=timeout, server_name=cfg.tls_server_name)


def kibana_client_from_config(cfg, *, insecure: bool = False) -> Client:
    verify = cfg.verify_certs and not insecure
    key, user, pw = cfg.kibana_api_key, "", ""
    if not key:  # default to the installer credential
        key, user, pw = cfg.install_api_key, cfg.username, cfg.password
        if not (key or user):
            key = cfg.api_key
    if not (key or user):
        raise ValueError("no Kibana credential configured")
    base = cfg.kibana_url + kibana_space_path(cfg.space)  # every /api/... call is then space-aware
    return Client(base, api_key=key, username=user, password=pw, ca_cert=cfg.kibana_ca_cert,
                  verify=verify, timeout=120.0, extra_headers={"kbn-xsrf": "kismet-cartographer"},
                  server_name=cfg.kibana_tls_server_name)


def kibana_space_path(space: str) -> str:
    """"" or "default" -> "" ; "team-a" -> "/s/team-a"."""
    space = (space or "").strip("/ ")
    return "" if space in ("", "default") else "/s/" + urllib.parse.quote(space, safe="")


def preflight(client: Client, aliases) -> tuple:
    """Check connectivity, auth and that the write aliases exist, using only index-level rights.

    Returns (version_or_None, missing_aliases). `GET /` needs cluster `monitor`, which the
    ingest key deliberately lacks, so a 403 there is fine: we then rely on view_index_metadata.
    Raises EsError/ConnectionFailure on real auth or connectivity problems.
    """
    version = None
    try:
        version = client.get("/")["version"]["number"]
    except EsError as e:
        if e.status != 403:
            raise
    found = client.get("/_alias/" + ",".join(aliases), ok=(200, 404))
    present = set()
    if isinstance(found, dict):
        for idx, body in found.items():
            if isinstance(body, dict):
                present.update((body.get("aliases") or {}).keys())
    return version, [a for a in aliases if a not in present]
