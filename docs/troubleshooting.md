# Troubleshooting

**`./ingest` says there are no files?** That is not an error: copy your `.kismet` files into the `wardrives/` folder and run it again.
**Says the indices do not exist?** Run `./install` first.

**Cannot connect, certificate errors, HTTP 401/403 or missing permissions?** Run `./install ... --check` and see the
[problem table in installation.md](installation.md#problems).

Start with the two commands that answer most questions:

```bash
python3 scripts/validate_elastic.py --source <dir-of-kismet-files> --expect-bbox <lat_min> <lat_max> <lon_min> <lon_max>
python3 scripts/inspect_kismet.py <file.kismet>          # what the capture itself contains
```

## Missing points on the map

Work down the list; each step rules out one cause.

1. **Time range.** Kibana's time picker filters on `@timestamp`. Use an absolute range that covers the dates you captured on
   (URL: `_g=(time:(from:'2026-01-01',to:'2026-02-01'))`). Device documents are stamped with their
   *last seen* time.
2. **The capture had no GPS fix.** This is the most common reason and is normal for indoor/stationary captures: Kismet writes `(0,0)`, the
   pipeline stores `geo.status: no_fix` and **no** `geo.location`, so the document exists but cannot be plotted.
   In Discover filter `geo.status : "no_fix"` and group by `kismet.source.file`; a large share of packets in a typical wardrive can be like this
   (a capture made without a GPS lock can have effectively none with a position).
   `python3 scripts/inspect_kismet.py <file>` shows `N are (0,0)=no fix`.
3. **The layer's filter.** Each Map layer shows one kind of record (`kismet.record`, `geo.location_type`). A Wi-Fi *device* is on the
   "devices - observation centroid" layer, not the "observations" layer. Aircraft are on the aircraft layer only.
4. **A global filter/control excludes it** (capture file, datasource, SSID ...). Clear the dashboard controls.
5. **Rejected coordinates.** `geo.status: invalid` with `geo.reason`:
   `out_of_range_or_not_numeric` (impossible lat/lon - Kismet ADS-B decode errors), `centroid_outside_device_bounds` (Kismet's garbage device average),
   `position_outside_device_bounds`, `implausible_range_from_collector` (aircraft > 1,000 km from the receiver). The value Kismet wrote is
   preserved in `geo.rejected`.
6. **Basemap blocked.** The Elastic Maps Service basemap is fetched by your *browser* from `maps.elastic.co`. If that is unreachable the points still render, on a blank background.
7. **Not indexed yet.** New documents become searchable within `refresh_interval` (30 s). Check the run summary and `out/rejects/`.

Count what exists: `GET kismet-cartographer-all/_search {"size":0,"aggs":{"by_file":{"terms":{"field":"kismet.source.file","size":100},"aggs":{"located":{"filter":{"exists":{"field":"geo.location"}}}}}}}`.

## Incorrect coordinates

* **Where are they?** `GET kismet-cartographer-all/_search {"size":0,"aggs":{"b":{"geo_bounds":{"field":"geo.location"}}}}` - the bounding box should be the area you drove.
  The validator's `--expect-bbox` flags collector positions outside it.
* **Compare a document with its source.** Every document carries `kismet.source.file`, `kismet.source.table` and `kismet.source.rowid`:
  ```bash
  sqlite3 -readonly "file:wardrives/X.kismet?mode=ro&immutable=1" \
    "select lat, lon, ts_sec from packets where rowid = 12345"
  ```
  The document's `geo.location.lat`/`.lon` must equal those columns (lat -> lat, lon -> lon). `validate_elastic.py --source` does this for a random sample.
* **Latitude and longitude swapped?** A swapped pair usually still passes range checks (e.g. 40.6 / -80.2 vs -80.2 / 40.6 is a legal point in Antarctica).
  The code never guesses order: SQL columns are `(lat, lon)`, Kismet JSON `geopoint` is `[lon, lat]` and is unpacked explicitly (tested in `tests/test_pipeline.py`).
  If you write your own tooling, always send `{"lat": .., "lon": ..}` objects, never bare arrays.
* **"The access point is not where the pin is."** By design: for Wi-Fi the location is the *collector's*. See `data-model.md` (`geo.location_type`).
* **A far-away aircraft.** `emitter_reported` positions are the aircraft's own; legitimate ones can be a few hundred km away. Impossible ones are rejected.

## Mapping failures

| Symptom | Cause / fix |
|---|---|
| Bulk item `mapper_parsing_exception ... failed to parse field [geo.location]` | Something wrote a non-point to `geo.location`. Only this project's pipeline should write these indices; check `out/rejects/*.ndjson` for the offending document. |
| `strict_dynamic_mapping_exception` | Not used - the indices are `dynamic: false` (unknown fields are stored, not indexed). |
| `_field_caps` shows `geo.location` with more than one type | Another index matching `kismet-cartographer-*` maps it differently (e.g. an unrelated stub). Query the alias `kismet-cartographer-all` instead of the wildcard, or fix/remove the other index. |
| `installer: mapping update rejected ... a type change needs a reindex` | You changed a field's type. Follow "Change a mapping type" in `operations.md`. |
| Kibana shows a field as *conflict* | Same cause as the `_field_caps` row; refresh the data view field list after fixing. |
| A field is present in `_source` but cannot be searched | It is not in the mapping (`dynamic: false`) - `kismet.raw` and `geo.rejected` are intentionally stored-only. Add the field to the component template, run `./install`, and re-ingest. |

## Connection, TLS and permission errors

| Message | Fix |
|---|---|
| `cannot connect ... CERTIFICATE_VERIFY_FAILED` | Set `ELASTIC_CA_CERT` (and `KIBANA_CA_CERT` - they differ on ECK) to the correct PEM; check the URL host matches a name/IP in the certificate. `--insecure` only for a throw-away test. |
| `HTTP 401` | Wrong, revoked or expired API key. Create a new one (`./install --print-api-key-request`). |
| `HTTP 403 ... action [indices:data/write/bulk[s]] is unauthorized` | The credential lacks `index`/`write`/`create_index` on `kismet-cartographer-*` (`installation.md`). |
| `HTTP 403 ... [cluster:monitor/main]` or `[indices:admin/refresh]` | Expected for the ingest key; the scripts do not need those. |
| `missing write alias(es) ... run scripts/install_elastic.py first` | The installer has not run (or ran against another cluster). |
| `refusing to overwrite ... not managed by this project` | A template/pipeline with the same name exists and is not ours. Rename or remove it deliberately; the installer will not. |
| Validator says ledger mismatch right after ingesting | Indices refresh every 30 s and the ingest key cannot force it; the validator waits one interval and re-checks. Re-run after a minute. |
| `cluster health yellow` | Replicas that cannot be assigned on a single node. The templates use 0 replicas for that reason. |
| `Elasticsearch ... too_many_requests` / 429 | The cluster is busy; bulk retries with backoff. Lower `--workers`. |

## Ingestion problems

* **Exit code 1 with `unparseable=N`.** Some source rows had invalid JSON; the documents are indexed from their columns with `kismet.parse_error`
  and every one is listed in `out/rejects/<capture>.<run>.rejects.ndjson`. Use `--allow-parse-errors` to accept.
* **`not a KismetDB file`.** The file has no `KISMET` table (wrong file, or a Kismet log of another type); it is skipped and the exit code is 1.
* **A file is still being written by Kismet.** Its hash changes on every run, so each snapshot becomes a separate set of documents. Ingest after Kismet closes it, or `--purge`.
* **Interrupted.** Just run it again; ids are deterministic. `--skip-complete` skips finished files.
* **Everything re-ingests although nothing changed.** The marker's `schema_version` differs from the code's (`SCHEMA_VERSION`) - expected after an upgrade.
* **Slow.** Use `--workers 3-4`; the source hash of a new file must be read once (cached afterwards in `state/hashes.json`).

## Still stuck

Run `python3 scripts/validate_elastic.py --json > validation.json` and `python3 scripts/inspect_kismet.py <file> --json > inspect.json`; between them they show the mapping, the counts,
the sentinel handling and what the capture contained.
