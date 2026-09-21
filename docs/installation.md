# Installation

The quick version is in the [README](../README.md). This page has the details for each setup.

You provide four things; the installer does everything else:

| You provide | Flag |
|---|---|
| Elasticsearch URL | `--es-url` (or `--host`, see below) |
| Kibana URL | `--kibana-url` (or `--host`) |
| An API key | `--api-key-file` (or `--api-key`, or the `ELASTIC_API_KEY` environment variable) |
| The CA certificate, if your stack uses HTTPS with a private CA | `--ca` (and `--kibana-ca` if Kibana uses a different one) |
| Your captures | copy the `.kismet` files into the **`wardrives/`** folder of this project (now or later; see "Two steps") |

## Two steps: install, then load captures

1. **`./install`** sets up Elasticsearch and Kibana. It does not need any captures.
2. **`./ingest`** loads the `.kismet` files in `wardrives/`. Run it once you have files, and again whenever you add more.

So you can install today and add captures next week. `./ingest` says clearly when `wardrives/` is empty (and that this is fine), and
tells you to run `./install` first if the indices do not exist yet. To avoid repeating the connection flags, give `./install` the
`--save-config` flag once; `./ingest` then needs none.

## Requirements

* **Python 3.9+** on the machine that runs the scripts. Nothing to `pip install`.
* **Elasticsearch and Kibana 8.x or 9.x.** Developed and tested on 9.4.2. Elasticsearch Serverless is not supported.
* Network access from that machine to Elasticsearch (usually port 9200) and Kibana (usually 5601).
* Disk: plan for roughly 15-30% of the size of your `.kismet` files as index storage (it varies with content). Kismet itself is not needed.

## 1. The API key

Run `./install --print-api-key-request`, paste the result into Kibana **Dev Tools -> Console** and run it. The answer contains an
`encoded` value; save it in a file (`chmod 600`). It is shown only once.

The key can do this and nothing more:

| Permission | Why |
|---|---|
| cluster `manage_index_templates`, `manage_ingest_pipelines` | create the mapping templates and the ingest pipeline |
| cluster `monitor` | read the version and count data nodes (to choose the number of replicas) |
| indices `kismet-cartographer-*`: `manage`, `create_index`, `read`, `write`, `view_index_metadata` | create, fill and read this project's indices only |
| Kibana `space_all` on one space | create the data view, map and dashboards |

The request sets a 90-day expiry; change or remove `"expiration"` as you like. When it expires, create a new key and use it instead.
If you already have an admin user, you can also make the key with `curl` or any client: the JSON is all that matters. The same key
is used for loading captures afterwards. If you want a narrower key for that (indices only), see [security.md](security.md).

**Whose key?** Any account's. The installer never needs a username: it sends the key, and Elasticsearch identifies the account
and its rights from it (the installer prints `authenticated as '<user>'` so you can see which one). The one rule is that the account
that creates the key must itself hold these rights, since a key cannot exceed its creator's. If your account lacks some of them, the
key is created without them and `--check` names each missing one. You can also create the key in Kibana under
**Stack Management -> API keys** by pasting the `role_descriptors` from the request.

Not sure the key is right? Run the installer with `--check`; it tells you exactly which permission is missing.

## 2. The CA certificate

Elasticsearch and Kibana are usually served with a certificate signed by a private CA that your computer does not know. Give the
installer that CA (a PEM file) and it verifies the connection properly. **Skip this if your certificate comes from a public CA**
(Elastic Cloud, or a Let's Encrypt certificate on an ingress).

| Setup | Where the CA is |
|---|---|
| **Standard install** (deb/rpm/zip) | `/etc/elasticsearch/certs/http_ca.crt` on the Elasticsearch host (copy it to your machine) |
| **Docker** | `docker cp <container>:/usr/share/elasticsearch/config/certs/http_ca.crt .` |
| **Kubernetes, ECK** | `kubectl -n <ns> get secret <es-name>-es-http-certs-public -o go-template='{{index .Data "ca.crt" \| base64decode}}' > es-ca.crt` |
| ECK, for Kibana | `kubectl -n <ns> get secret <kb-name>-kb-http-certs-public -o go-template='{{index .Data "ca.crt" \| base64decode}}' > kibana-ca.crt` |
| **Your own PKI** | your organisation's CA certificate |
| No access to the cluster | `openssl s_client -connect host:9200 -showcerts </dev/null` prints the chain; take the last certificate and confirm its fingerprint with your administrator |

On ECK, Elasticsearch and Kibana have **different** CAs unless you supplied your own: pass `--ca es-ca.crt --kibana-ca kibana-ca.crt`.
If both use the same CA, `--ca` alone is enough.

## Reaching your stack

Use the addresses your computer can reach.

* **No HTTPS (plain HTTP):** you do not need a CA. Give the machine's address instead:
  ```bash
  ./install --host 192.168.1.20 --api-key-file ~/kc.key
  ```
  This uses `http://192.168.1.20:9200` and `http://192.168.1.20:5601`. Different ports: `--es-port 19200 --kibana-port 15601`.
  Only Elasticsearch on HTTPS (or the reverse)? Use `--es-url` and `--kibana-url` for each. `--host` uses `https://` instead when
  you add `--https` or `--ca`. **The API key and your data then cross the network unencrypted**, so use it on a trusted network only
  (the installer warns you). API keys still need Elasticsearch security to be enabled.

* **Standard install:** `https://<host>:9200` and `https://<host>:5601` (or `http://` if Kibana is not using TLS).
* **Kubernetes with an ingress or load balancer:** use those public names. Nothing else to do.
* **Kubernetes with `kubectl port-forward`:**
  ```bash
  kubectl -n <ns> port-forward svc/<es-name>-es-http 9200 &
  kubectl -n <ns> port-forward svc/<kb-name>-kb-http 5601 &
  ./install --es-url https://localhost:9200 --kibana-url https://localhost:5601 \
            --api-key-file ~/kc.key --ca es-ca.crt --kibana-ca kibana-ca.crt \
            --tls-server-name <es-name>-es-http.<ns>.svc \
            --kibana-tls-server-name <kb-name>-kb-http.<ns>.svc
  ```
  The certificates are issued for the in-cluster service names, not `localhost`, so these two flags tell the installer which name to
  verify for each service. The connection is still fully verified.
* **Kibana under a base path** (for example `https://example.com/kibana`): include the path in `--kibana-url`.
* **A Kibana space:** `--space <id>` installs into that space instead of the default one. Create the API key for the same space
  (`./install --print-api-key-request --space <id>`). The space must already exist.
* **Elastic Cloud (hosted):** use the endpoint URLs from the Cloud console; no `--ca` is needed.

## 3. Run the installer

```bash
./install --es-url URL --kibana-url URL --api-key-file FILE [--ca FILE] [--kibana-ca FILE]
```

| Option | What it does |
|---|---|
| `--check` | test the connection and the key's permissions only; change nothing |
| `--dry-run` | run every step but report what would be created or changed instead of doing it |
| `--space ID` | Kibana space (default: the default space) |
| `--replicas N` | replicas for the new indices. Default: 1 when the cluster has two or more data nodes, otherwise 0 |
| `--skip-kibana` | Elasticsearch only |
| `--host ADDRESS` | build both URLs from one address (`http://` unless you add `--https` or `--ca`); ports with `--es-port` / `--kibana-port` |
| `--ingest [PATH]` / `--validate` | also load captures right away (from `PATH`, or the `wardrives/` folder), then compare the stored data with the source files. Same as running `./ingest` afterwards; if the folder is empty the install still succeeds |
| `--tls-server-name NAME`, `--kibana-tls-server-name NAME` | certificate name to verify when the URL uses a different name (port-forward) |
| `--insecure` | do not verify certificates. Only for throw-away tests; traffic can then be read or changed by anyone on the path |
| `--save-config [FILE]` | after a successful install, save the settings (URLs, CA paths, space, and the *path* of your key file, never the key) to `FILE`, default `.env` in this folder (mode 600, git-ignored). `./ingest` and the other scripts read it automatically |
| `--env-file FILE` | read `ELASTIC_URL`, `ELASTIC_API_KEY`, `ELASTIC_CA_CERT`, `KIBANA_URL`, `KIBANA_CA_CERT`, `KIBANA_SPACE` from a file (see `.env.example`) |
| `--test` | run the offline unit tests first |

Flags win over environment variables, which win over env files. Put the key in a file or the environment rather than on the
command line: `--api-key` is visible to other users in `ps` and stays in your shell history.

What it does, in order (each step can be repeated safely; running it again is how you upgrade):

1. **Checks** - Python version, connection and certificate, that the key is accepted and has every permission it needs,
   that Kibana is reachable and accepts the key. Nothing is changed until all of this passes, and any failure says how to fix it.
2. **Elasticsearch** - component and index templates, the ingest pipeline, the four indices with their aliases, then a self-test of
   the pipeline. Existing resources that are not tagged as this project's are never overwritten.
3. **Kibana** - finds or creates the `kismet-cartographer-*` data view, then creates or updates the map, dashboards and
   visualizations. Objects that are already up to date are left alone.
4. **Optional** - loads your captures and validates them (`--ingest`, or later with `./ingest`).

Exit codes: `0` success, `1` a step failed, `2` a usage or configuration problem, `3` could not connect, authenticate or lacking permission.

## Problems

| Message | Cause and fix |
|---|---|
| `certificate verify failed ... self-signed certificate` | The CA is missing or wrong. Pass `--ca` (and `--kibana-ca` for Kibana). |
| `certificate is not valid for '<name>'` | You connect through another name than the certificate lists. Use `--tls-server-name` / `--kibana-tls-server-name`. |
| `HTTP 401` | Wrong, revoked or expired key. Pass the `encoded` value from step 1. |
| `missing permissions: ...` | The key lacks the listed rights. Create it with `./install --print-api-key-request`. |
| `Kibana answered 404` | Wrong Kibana URL (or a missing base path). |
| `Kibana space '...' does not exist` | Create the space first, or omit `--space`. |
| `seems to require HTTPS`, or `Connection reset` / `hung up` on an `http://` URL | You used `http://` (or `--host` alone) but the stack uses HTTPS. Add `--https` and `--ca <file>`. |
| `wrong version number` on an `https://` URL | The opposite: the stack is plain HTTP. Use `--host` without `--https`, or an `http://` URL. |
| `did not answer like Elasticsearch` | The URL points at Kibana or something else; use the Elasticsearch port. |

## `./ingest`

```bash
./ingest                       # everything in wardrives/ (uses the settings saved by ./install --save-config)
./ingest --es-url ... --api-key-file ... --ca ...      # or give the connection flags again
./ingest path/to/file.kismet   # one file, or another folder
./ingest --check               # test the connection and permissions and list the files, load nothing
./ingest --validate            # after loading, compare what was stored with the source files
./ingest --reload              # process files again even if they were already loaded
```

It needs less than `./install`: a key that may read and write the `kismet-cartographer-*` indices is enough (see the narrower key in
[security.md](security.md)). Exit code `0` also when there is nothing to load yet.

More in [troubleshooting.md](troubleshooting.md). To remove everything, see [operations.md](operations.md).
