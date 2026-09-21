# Architecture

Kismet Cartographer turns KismetDB capture files (`*.kismet`, SQLite) into geospatial documents in
Elasticsearch and a Kibana Map/Dashboard. It is a batch pipeline: nothing runs continuously, the
originals are never modified, and every step can be repeated.

```
                       read-only, immutable                          HTTPS (TLS verified against your CA)
 ┌──────────────┐      ┌────────────────────────────────────┐      ┌──────────────────────────────────────┐
 │ *.kismet     │      │ kismet_cartographer (Python stdlib)│      │ Elasticsearch 8.x / 9.x              │
 │ KismetDB     │─────▶│                                    │─────▶│                                      │
 │ (SQLite v9)  │      │ 1 extract   kismetdb.py            │ bulk │ ingest pipeline                      │
 │  devices     │      │ 2 normalise normalize.py + geo.py  │      │   kismet-cartographer-normalize-v1   │
 │  packets     │      │ 3 validate  (per-row, sentinels)   │      │        │                             │
 │  data        │      │ 4 bulk      bulk.py  (idempotent)  │      │        ▼                             │
 │  alerts      │      └────────────────────────────────────┘      │ kismet-cartographer-observations-v1  │
 │  messages    │       scripts/ingest_kismet.py                   │ kismet-cartographer-devices-v1       │
 │  snapshots   │       scripts/export_kismet.py (offline NDJSON)  │ kismet-cartographer-track-v1         │
 │  datasources │                                                  │ kismet-cartographer-system-v1        │
 └──────────────┘                                                  │  (write alias = name without -v1,    │
        ▲                                                          │   read alias kismet-cartographer-all)│
        │ never written                                            └──────────────┬───────────────────────┘
        │                                                                         │ geo.location : geo_point
        │        scripts/validate_elastic.py  ◀── compares ES to the source ──────┤
        │                                                                         ▼
        │                                                          ┌──────────────────────────────────────┐
        └────── sampled rows compared ─────────────────────────────│ Kibana: data view, Map, Dashboard    │
                                                                   └──────────────────────────────────────┘
```

## Components

| Component | File | Responsibility |
|---|---|---|
| KismetDB reader | `kismet_cartographer/kismetdb.py` | Opens files `mode=ro&immutable=1` (SQLite never creates side files), reads tables tolerant of missing columns, streams rows in batches, SHA-256 of the file (cached in `state/hashes.json`). |
| Normaliser | `kismet_cartographer/normalize.py` | One builder per table -> documents. Applies the sentinel rules, position semantics, derived-field bookkeeping, provenance. |
| Geo rules | `kismet_cartographer/geo.py` | The only place that decides whether a coordinate is real. |
| Bulk writer | `kismet_cartographer/bulk.py` | Threaded `_bulk`, deterministic `_id`, per-item retry on 429/503, rejects captured. |
| Orchestration | `kismet_cartographer/ingest.py` | Per-file flow, progress, rejects log, completion marker. |
| HTTP client | `kismet_cartographer/esclient.py` | stdlib only; verified TLS; retry/backoff; `require_owned()` name guard. |
| Config | `kismet_cartographer/config.py` | Secrets from environment / protected file only. |
| Elasticsearch resources | `config/elasticsearch/**`, `scripts/install_elastic.py` | Component templates, index templates, ingest pipeline, indices, aliases. |
| Kibana objects | `config/kibana/**`, `scripts/install_kibana.py` | Data view, Map, Dashboard (see `kibana-maps.md`). |
| Entry points | `install`, `scripts/*.py` | See `installation.md` and `ingestion.md`. |

## Why these choices

* **Read KismetDB directly instead of `kismetdb_dump_devices --ekjson`.** The Kismet utilities are
  not required and are not installed on the reference machine. More importantly, `--ekjson` covers
  only the device table; the position-bearing per-packet rows (`packets`), ADS-B frames (`data`),
  the GPS track (`snapshots`), alerts and messages need direct SQL anyway. Every BLOB column that
  matters is plain JSON, so no decompression is needed.
  The Kismet tools remain useful as an optional independent cross-check (`installation.md`).
* **Python standard library only.** An installer for other administrators must not depend on pip,
  a virtualenv or a compiler. `urllib` + `ssl` + `sqlite3` + `json` are enough.
* **Four indices, one shared mapping.** The families have different lifecycles and sizes
  (millions of observation rows vs. hundreds of thousands of devices) but share one field
  vocabulary, so a single data view and one set of Kibana filters work across all of them. Each
  index has 1 primary shard and 0 replicas so the project stays small on a shard-constrained
  single-node cluster (`operations.md`).
* **Regular indices + aliases instead of data streams.** The data is an archive, not a live stream:
  documents are keyed by deterministic `_id` and re-ingestion overwrites them. Data streams accept
  only `create`, cannot be overwritten by `_id`, and need `manage` for rollover; plain indices with
  a write alias give idempotent re-runs and let an operator move to `-v2` without touching the
  ingester (`operations.md`).
* **Deterministic IDs.** `_id = base64url(sha256(file_sha256 | table | row key))[:15 bytes]`. The
  same file content always maps to the same documents, whatever its name or location.
* **`dynamic: false`.** Critical fields (notably `geo.location`) are mapped explicitly and unknown
  fields never create mappings. The original Kismet JSON is preserved in `kismet.raw`, which is
  stored in `_source` but not indexed (13 KiB per device on average would otherwise explode the
  field count).
* **Least privilege.** One API key limited to `kismet-cartographer-*` indices and one Kibana space does the install;
  a narrower indices-only key can be used just for loading captures. See `security.md`.

## Data flow for one file

1. `KismetDb` opens the file read-only, checks it has a `KISMET` table, records `kismet_version`,
   `db_version`, and computes/looks up the file SHA-256.
2. Tables are processed in dependency order: `datasources` (name/type map) -> `devices` (fills a
   MAC -> device map used to link packets/frames within the same file) -> `packets` -> `data` ->
   `alerts` -> `messages` -> `snapshots`.
3. Each row becomes one document (or is reported); documents stream into a bounded queue of
   `_bulk` requests.
4. The ingest pipeline `kismet-cartographer-normalize-v1` re-checks geo points, drops a `0` RSSI
   and stamps `event.ingested`, so even a foreign writer cannot put a `(0,0)` point in the index.
5. When all tables are done a completion marker (`kismet.record: ingest.run`) with per-table
   counts is written. `validate_elastic.py` reconciles those counts with what is indexed.

## Known limitations

* **Wi-Fi transmitter location is not known.** Kismet records where the *collector* was. The
  pipeline never presents a collector position as an access point's position (`data-model.md`).
* **Packet payload bytes are not copied.** `packets.packet` (raw radiotap frames, a large part of a
  capture file) is not indexed; length, hash, DLT and a pointer back to the source row
  (`kismet.source.file`, `kismet.source.rowid`) are kept.
* **Kismet data-quality problems are handled, not fixed at the source.** `(0,0)` "no fix" values, `signal = 0`, garbage device centroids,
  impossible or implausible ADS-B positions and absurd aircraft altitudes are detected and kept out of the searchable fields; the original values are preserved
  (`geo.rejected`, `aircraft.rejected`, `kismet.raw`, `kismet.packet.signal_raw`). The thresholds (1,000 km ADS-B range, -1,500..30,000 m altitude)
  are constants in `kismet_cartographer/constants.py`. See `data-model.md`.
* **Reversed coordinates inside valid ranges cannot be detected by range checks.** They are
  prevented by construction (column order is fixed in code, JSON `[lon, lat]` is unpacked
  explicitly) and verified by `validate_elastic.py --source`, which compares sampled documents to the
  original rows.
* **Growing capture files.** A file that Kismet is still writing has a different SHA-256 each time,
  so each snapshot is a different set of documents. Ingest completed captures, or use `--purge`.
* **Time is UTC epoch from Kismet.** Timestamps are indexed as ISO-8601 UTC with millisecond
  precision; the microsecond part is kept in `kismet.ts.usec`.
* **Cluster-wide template/pipeline privileges are not name-scoped by Elasticsearch.** The scripts
  enforce the `kismet-cartographer-` prefix themselves; the credential you give them could still
  do more if misused. Use a dedicated credential (`security.md`).
