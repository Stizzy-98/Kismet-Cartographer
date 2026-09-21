# Kismet Cartographer

A painless process to load [Kismet](https://www.kismetwireless.net/) wardriving captures (`*.kismet` files) into **Elasticsearch** and explore them
in **Kibana**: a map of where you captured data, plus dashboards that show which brands, models and security versions of Wi-Fi
devices were around you.

* One command installs everything into your Elastic Stack - standard install, Kubernetes (ECK) or Elastic Cloud.
* You only need an API key, the CA certificate (if your stack uses a private CA, or just the address if it does not use HTTPS) and the two URLs.
* Python 3.9+ standard library only. No pip packages, and no Kismet installation needed.
* Your original `.kismet` files are never modified. Everything created in Elastic is named `kismet-cartographer-*`;
  nothing else in your cluster is touched.

> **Privacy and legality.** Captures contain your movements and other people's network names and device MAC addresses.
> Only collect data you are allowed to collect, and keep the resulting indices restricted. See [docs/security.md](docs/security.md).

## Install

You need Elasticsearch and Kibana **8.x or 9.x** (developed and tested on 9.4) and a machine with Python 3.9+ that can reach both.

### 1. Create an API key

Create a API key with these permissions:
```
{
  "kismet-cartographer-ingest": {
    "cluster": [],
    "indices": [
      {
        "names": [
          "kismet-cartographer-*"
        ],
        "privileges": [
          "create_index",
          "create_doc",
          "index",
          "write",
          "read",
          "view_index_metadata"
        ],
        "allow_restricted_indices": false
      }
    ],
    "applications": [],
    "run_as": [],
    "metadata": {},
    "transient_metadata": {
      "enabled": true
    }
  }
}
```

The key can belong to **any account**: `elastic`, your own user, or a service account. The installer never asks whose key it is;
Elasticsearch works that out from the key. The only requirement is that the account creating it holds those rights itself
(an admin such as `elastic` does), because a key can never have more rights than the account that made it.

### 2. Get the CA certificate

Skip this step if your stack uses a certificate from a public CA (Elastic Cloud does), or if it does not use HTTPS at all (see the `--host` option in step 3).

| Your stack | How to get the CA |
|---|---|
| Standard install (package / archive) | `/etc/elasticsearch/certs/http_ca.crt` on the Elasticsearch host |
| Docker | `docker cp <container>:/usr/share/elasticsearch/config/certs/http_ca.crt .` |
| Kubernetes with ECK | `kubectl -n <ns> get secret <name>-es-http-certs-public -o go-template='{{index .Data "ca.crt" \| base64decode}}' > ca.crt` |

On ECK, Kibana has its own CA (secret `<name>-kb-http-certs-public`); pass it with `--kibana-ca`. More options in
[docs/installation.md](docs/installation.md).

### 3. Run the installer

```bash
./install \
  --es-url     https://elasticsearch.example.com:9200 \
  --kibana-url https://kibana.example.com:5601 \
  --api-key-file ~/kc.key \
  --ca ./ca.crt \
  --save-config
```

`--save-config` remembers these settings (in a private, git-ignored `.env`; **never the key itself**, only where its file is) so the
next step needs no flags at all.

No HTTPS on your stack? Skip the CA and give the address instead (add `--es-port` / `--kibana-port` if yours are not 9200 / 5601):

```bash
./install --host 192.168.1.20 --api-key-file ~/kc.key
```

That connection is unencrypted, so use it only on a network you trust.

The installer first checks the connection, the certificate and the key's permissions (nothing is changed until they pass), then creates
the templates, pipeline and indices, and imports the data view, map and dashboards. It is safe to run again; that is also how
you upgrade. Useful extras: `--check` (only test), `--dry-run` (show what would change), `--kibana-ca`, `--space`.

**You do not need any captures yet.** The install works with an empty `wardrives/` folder; loading data is a separate step (next).

Running Kubernetes and reaching the cluster with `kubectl port-forward`? See
[docs/installation.md](docs/installation.md#reaching-your-stack).

### 4. Load your captures (now or any time later)

**Put all your `.kismet` files in the `wardrives/` folder** of this project. This is where the scripts look by default
(Kismet names them like `Kismet-20260101-12-00-00-1.kismet`). The folder's contents are never committed to git.

Then run:

```bash
./ingest
```

That is the whole step if you used `--save-config`; otherwise add the same connection flags you gave `./install`. It checks the
connection and permissions, loads every file, and skips files it has already loaded, so it is quick and safe to repeat.
**Whenever you have new captures, copy them into `wardrives/` and run `./ingest` again.** Options: `--check` (test only),
`--validate` (compare what was stored with your files), `--reload` (process everything again), or a path to load a single file or
another folder. Expect several thousand documents per second.

No captures yet, and want to see the dashboards? `python3 scripts/make_sample_capture.py` writes a small invented one into
`wardrives/`; `./ingest` loads it. (Remove it when you are done so it does not mix with real data.)

### 5. Open Kibana

**Dashboards -> "Kismet Cartographer - Overview"** (map and counts) and **"Kismet Cartographer - Devices & Hardware"**
(who makes the devices around you). Set the time range to the dates you captured on.

## What you get

* **Map** - your route and the Wi-Fi observations along it. Wi-Fi points are *where you were* when a device was heard, never
  presented as the device's location. Captures without a GPS fix are counted but not plotted.
* **Overview dashboard** - totals, observations over time, signal strength, data sources, filters for capture file, SSID, band and more.
* **Devices & Hardware dashboard** - brands, models, device types, and security versions (WPA2 / WPA3 / open) of Wi-Fi devices.

## Documentation

| | |
|---|---|
| [docs/installation.md](docs/installation.md) | the API key, CA certificates, Kubernetes and other setups, all installer options |
| [docs/ingestion.md](docs/ingestion.md) | loading captures, re-running, options |
| [docs/kibana-maps.md](docs/kibana-maps.md) | the map, dashboards and filters |
| [docs/data-model.md](docs/data-model.md) | indices and fields, how positions and brands are derived |
| [docs/troubleshooting.md](docs/troubleshooting.md) | connection errors, missing points, wrong coordinates |
| [docs/testing.md](docs/testing.md) | the unit tests, trying it without captures, checking a real load |
| [docs/security.md](docs/security.md) | permissions, credentials, TLS, sensitivity of the data |
| [docs/operations.md](docs/operations.md) | upgrades, reindexing, removing data, sizing |
| [docs/architecture.md](docs/architecture.md) | how the pieces fit together |

## Elasticsearch and Kibana objects created

| Kind | Names |
|---|---|
| Indices | `kismet-cartographer-{observations,devices,track,system}-v1` (aliases without the suffix; read alias `kismet-cartographer-all`) |
| Templates and pipeline | component templates `kismet-cartographer-settings-v1`, `-mappings-common-v1`; index templates `kismet-cartographer-*`; ingest pipeline `kismet-cartographer-normalize-v1` |
| Kibana | data view `kismet-cartographer-*` (reused if it exists), the Map, the two dashboards and their visualizations |

## Repository layout

```
install                       step 1: the installer
ingest                        step 2 (any time later): load the captures in wardrives/
wardrives/                    put your .kismet capture files here
kismet_cartographer/          the library (standard library only)
scripts/                      ingest_kismet.py, validate_elastic.py, inspect_kismet.py, make_sample_capture.py, export_*.py, install_*.py
config/                       Elasticsearch templates/pipeline and Kibana saved objects (plain JSON)
docs/  tests/
```

## Tests

```bash
python3 -m unittest discover -s tests
```

Runs offline in under a second and needs nothing of yours: the pipeline tests use a small invented capture built on the fly.
See [docs/testing.md](docs/testing.md).

## Known limitations

* Wi-Fi transmitter positions are unknown: only where the collector was is recorded, and it is labelled that way.
* Raw packet bytes are not copied; a pointer to the source row is kept.
* Brand and model are what a device announces or what its MAC address registry says; models are known for a minority of devices (those that announce one).
* Developed and tested on Elasticsearch/Kibana 9.4.2. Other 8.x/9.x versions should work but are untested. Elasticsearch Serverless is not supported.
* The Kibana map basemap is loaded by your browser from the Elastic Maps Service.

Kismet is a trademark of its authors; this project is an independent tool and is not affiliated with Kismet or Elastic.
