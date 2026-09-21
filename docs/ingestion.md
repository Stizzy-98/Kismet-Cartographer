# Ingestion

Put your `.kismet` files in the **`wardrives/`** folder of this project and run **`./ingest`**. It loads everything in that folder,
skips files that were already loaded, and is safe to repeat: run it again whenever you add captures. You can do this any time after
`./install`, even weeks later. `./ingest` needs no flags if you ran `./install --save-config`; otherwise give the connection flags
from [installation.md](installation.md) (`--es-url`, `--api-key-file`, `--ca`, or `--host`).

The rest of this page is about `scripts/ingest_kismet.py`, the loader `./ingest` runs for you (use it directly for its extra options).

## Inspect first (optional, read-only)

```bash
python3 scripts/inspect_kismet.py wardrives/Kismet-YYYYMMDD-HH-MM-SS-1.kismet  # versions, tables, datasources, GPS/sentinel stats
python3 scripts/inspect_kismet.py wardrives/ --summary                             # one line per file
python3 scripts/inspect_kismet.py capture.kismet --json --full                     # exact stats, machine-readable
```

## Ingest one file

```bash
python3 scripts/ingest_kismet.py wardrives/Kismet-YYYYMMDD-HH-MM-SS-1.kismet \
  --es-url https://es.example.com:9200 --api-key-file ~/kc.key --ca ca.crt
```

## Ingest a directory

```bash
python3 scripts/ingest_kismet.py wardrives/ --skip-complete --workers 3
```

Files are processed in name order; a failure in one file is reported and the run continues with the next.
Progress (`docs/s`) is printed per file and a summary table at the end.

## Options

| Option | Meaning |
|---|---|
| `--tables devices,packets,...` | limit to some tables (`datasources,devices,packets,data,alerts,messages,snapshots`) |
| `--limit N` | at most N rows per table (dry runs / tests) |
| `--workers N`, `--batch-docs N` | parallel `_bulk` requests (default 2) and docs per request (default 5000). Keep workers low on shared clusters. |
| `--op index` (default) / `--op create` | `index` overwrites documents with the same `_id`; `create` skips ones that already exist (409 counted as "existing") |
| `--skip-complete` | skip files whose completion marker (`ingest.run`, same `schema_version`) is already indexed |
| `--purge` | delete every document from this file's content hash first, then ingest (use when a document shape changed) |
| `--allow-parse-errors` | exit 0 when the only problem is unparseable source rows (they are still indexed and logged) |
| `--rejects-dir DIR` | where reject logs go (default `out/rejects/`) |
| `--quiet` | no progress output |
| `--es-url`, `--api-key-file`, `--ca`, `--tls-server-name`, `--insecure`, `--env-file` | how to connect: the same flags as `./install` (see `installation.md`) |

## Offline export (no Elasticsearch)

```bash
python3 scripts/export_kismet.py wardrives/capture.kismet -o out/export     # bulk NDJSON, one file per capture
curl --cacert ca.crt -H "Authorization: ApiKey $KEY" -H 'Content-Type: application/x-ndjson' \
     -X POST "$ELASTIC_URL/_bulk" --data-binary @out/export/capture.kismet.bulk.ndjson
```

## How duplicate ingestion is prevented

* `_id` is derived from the **file content hash + table + row key** (`data-model.md`). Ingesting the same file again - even from a
  different path or under a different name - writes to the same ids and **overwrites** instead of adding.
* `--skip-complete` avoids even re-sending: each finished file leaves a marker document; a re-run of the directory skips them
  in milliseconds. A marker from an older `schema_version` is not trusted, so an upgrade re-ingests automatically.
* An interrupted run is safe to repeat (no marker is written until every table is done).
* Two captures that happen to contain the same real-world device are separate documents (they are different observations
  in different files); filter by `kismet.source.file`.

Verified: ingesting the same file twice leaves the index at exactly the same number of documents (one per source row plus one completion marker).

## What is reported

For every file: rows read, documents built/indexed/rejected, documents without GPS, unparseable rows, and the run status
(`complete`, `partial` if Elasticsearch rejected some documents, `failed`). Details go to
`out/rejects/<capture>.<run>.rejects.ndjson`, one JSON object per line:

```json
{"file": "x.kismet", "stage": "parse", "table": "devices", "rowid": 40, "error": "device json: invalid JSON: ..."}
{"file": "x.kismet", "stage": "index", "index": "kismet-cartographer-observations", "id": "...", "status": 400, "error_type": "...", "reason": "..."}
```

A source row whose JSON is broken is still indexed from its ordinary columns (with `kismet.parse_error`); a row that raises while being
built is listed and skipped. **Records without GPS are counted, not hidden** (`no_gps=` in the summary; `geo.status: no_fix` in the index).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every document indexed (and no unparseable rows unless `--allow-parse-errors`) |
| 1 | some rows unparseable, some documents rejected, a file failed, or the run was interrupted |
| 2 | usage/config error, missing aliases (run `./install` first), missing file |
| 3 | cannot connect / TLS verification failed / authentication or authorisation failed |

## Performance

Typically several thousand documents per second with `--workers 3`; a multi-gigabyte collection takes minutes to tens of minutes,
depending on your cluster. The originals are read sequentially and the SHA-256 of each file is cached in
`state/hashes.json` (keyed by path/size/mtime). Memory stays flat: at most `workers x 2` batches are in flight.
Because `refresh_interval` is 30 s, new documents become searchable within half a minute of the last batch.

## Validate

```bash
python3 scripts/validate_elastic.py --source wardrives/ --sample 300 --expect-bbox 36 41 -81 -74
```

Checks the mapping, coordinate validity, timestamps, provenance, the per-file ledger (expected vs. indexed counts) and - with
`--source` - compares randomly sampled documents with the original KismetDB rows (`test-results.md`).


## Export to WiGLE (CSV)

```bash
python3 scripts/export_wigle.py wardrives/ -o exports/wigle/kismet-wardrives-wigle.csv
```

Produces one WiGLE `WigleWifi-1.4` CSV from every capture in the directory (upload at
<https://wigle.net/uploads>). No Elasticsearch is needed; the `.kismet` files are opened read-only and
are never modified or deleted. The output folder `exports/` is git-ignored (real positions and SSIDs).

* **Rows**: one per access point (`type = "Wi-Fi AP"`) and per SSID it advertised, per capture file. The
  position, time, RSSI and altitude are those of the packet with the **strongest signal** that carries a real
  GPS fix (collector position - WiGLE estimates the AP location from this). Client devices are not exported.
* **Attribution**: this Kismet version leaves `packets.devkey` empty, so a packet belongs to an AP when its
  transmitting address (`sourcemac`) is the AP's BSSID (beacons, probe responses, AP-originated data).
* **Not exported (counted in the report, never silent)**: APs never heard with a GPS fix (`ap_no_position`)
  or with a fix but no signal (`ap_no_signal`). `(0,0)` positions and `signal = 0` are Kismet "absent"
  sentinels and are never used.
* **Columns**: `MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type`.
  `FirstSeen` is the UTC time of the chosen observation. `AccuracyMeters` is `0` (Kismet does not record accuracy).
  `Type` is `WIFI`. `AuthMode` is translated from Kismet's crypt string (`WPA2 WPA2-PSK AES-CCMP` ->
  `[WPA2-PSK-CCMP][ESS]`); an empty crypt string or `Open` becomes `[ESS]`; a string that cannot be translated
  (e.g. `AES-BIP-CMAC256` alone) becomes `[ESS]` and is counted as `unrecognised_auth`. Control characters in an SSID
  would be replaced by a space (counted).
