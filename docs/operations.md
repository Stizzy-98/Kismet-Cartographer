# Operations

The scripts take the same connection flags as `./install` (`--es-url`, `--api-key-file`, `--ca`, ...; see `installation.md`).
For the raw `curl` examples below, replace `$ES` with your Elasticsearch URL and add `--cacert <ca.crt>` and your API key header.

## Routine

| Task | Command |
|---|---|
| Ingest new captures | `python3 scripts/ingest_kismet.py <dir> --skip-complete` |
| Check health of the data | `python3 scripts/validate_elastic.py --source <dir>` |
| See per-file ingest ledger | `GET kismet-cartographer-system/_search {"query":{"term":{"kismet.record":"ingest.run"}}}` |
| See what a capture contains | `python3 scripts/inspect_kismet.py <file>` |
| Renew the API key before it expires | create a new one (`./install --print-api-key-request`), switch to it, then invalidate the old one (`security.md`) |

## Upgrade the pipeline

1. `git pull` / replace the project files.
2. Run the unit tests: `python3 -m unittest discover -s tests`.
3. `./install <your connection flags>` - re-applies templates, pipeline and mappings (additive mapping changes are applied to the existing indices in
   place; the installer stops with a clear message if a change needs a reindex).
4. If document *content* changed (`SCHEMA_VERSION` in `kismet_cartographer/__init__.py` is bumped), re-ingest: because markers
   carry the schema version, `ingest_kismet.py <dir> --skip-complete` re-processes exactly the stale files, overwriting documents
   in place (same `_id`s).
5. `python3 scripts/validate_elastic.py --source <dir>`.

## Change a mapping type (needs a reindex)

Field types cannot be changed in place.

1. Edit `config/elasticsearch/component-templates/kismet-cartographer-mappings-common-v1.json`; create new `-v2` indices from the
   updated template: `PUT kismet-cartographer-observations-v2` (repeat per family).
2. `POST _reindex {"source":{"index":"kismet-cartographer-observations-v1"},"dest":{"index":"kismet-cartographer-observations-v2"}}`
   (or simply re-ingest from the `.kismet` files - they are the source of truth and re-ingestion is idempotent).
3. Move the write alias atomically:
   ```
   POST _aliases { "actions": [
     {"remove": {"index": "kismet-cartographer-observations-v1", "alias": "kismet-cartographer-observations"}},
     {"add":    {"index": "kismet-cartographer-observations-v2", "alias": "kismet-cartographer-observations", "is_write_index": true}},
     {"add":    {"index": "kismet-cartographer-observations-v2", "alias": "kismet-cartographer-all"}} ] }
   ```
4. Validate, then delete the `-v1` index. Update `FAMILIES`/`CONCRETE_VERSION` in `kismet_cartographer/constants.py` so the
   installer manages `-v2`.

## Remove data safely

**One capture file** (documents + its ingest marker; needs `write`):
```bash
H=$(sha256sum wardrives/X.kismet | cut -d' ' -f1)
curl --cacert ca.crt -H "Authorization: ApiKey $KEY" -H 'Content-Type: application/json' -X POST \
  "$ES/kismet-cartographer-*/_delete_by_query?conflicts=proceed&wait_for_completion=true" \
  -d "{\"query\":{\"term\":{\"kismet.source.file_hash\":\"$H\"}}}"
```
(or `ingest_kismet.py X.kismet --purge` to replace it). First run the same query with `_count` to see what would be deleted.

**All data, keep the structure:** `POST kismet-cartographer-*/_delete_by_query {"query":{"match_all":{}}}` with the pattern limited to
`kismet-cartographer-observations-v1,...` (do **not** use `kismet-cartographer-*` if another team keeps their own indices under that prefix; the
installer's `FAMILIES` list is the authoritative set of indices this project owns).

**Remove everything this project created** (needs the installer key; nothing outside the prefix is touched):
```
DELETE kismet-cartographer-observations-v1,kismet-cartographer-devices-v1,kismet-cartographer-track-v1,kismet-cartographer-system-v1
DELETE _index_template/kismet-cartographer-observations   (and -devices, -track, -system)
DELETE _component_template/kismet-cartographer-mappings-common-v1     DELETE _component_template/kismet-cartographer-settings-v1
DELETE _ingest/pipeline/kismet-cartographer-normalize-v1
```
Delete templates only after the indices (a component template in use cannot be deleted). Kibana: delete the objects titled
"Kismet Cartographer ..." (Stack Management -> Saved Objects); leave the data view unless you created it. Do not delete an
index that you did not create (e.g. an unrelated stub named `kismet-cartographer-events`).

## Sizing and cluster limits

* `kismet.raw` (the original device JSON, on the order of 10 KiB per device) is the bulk of `kismet-cartographer-devices-v1`.
* Shards: 4 primaries, 0 replicas. Elasticsearch defaults to at most 1,000 shards per data node; check headroom first
  (`GET _cluster/health` -> `active_shards + unassigned_shards`).
* Replicas: set `./install --replicas 1` on multi-node clusters. On a single node a replica can never be assigned and only turns the
  cluster yellow.
* `refresh_interval` is 30 s (bulk-friendly); lower it on the index if you need faster visibility.
* Lifecycle: the data is an archive, so no ILM policy is installed. Add one yourself if you need retention.

## Backup

Snapshot the four indices with your normal snapshot repository; the `.kismet` files are the source of truth and
re-ingestion is idempotent, so also keep them. Snapshots contain the sensitive data described in `security.md`.

## Auditing unmapped fields

`python3 scripts/audit_unmapped.py` (read-only; the indices-only key is enough) samples stored documents per index and
record type and lists `_source` fields that have no mapping. Fields under the deliberately stored-only objects
(`kismet.raw`, `geo.rejected`, `aircraft.rejected`, `kismet.ingest.tables/options`, all `enabled: false`) are
expected to be unmapped and are listed separately. Apart from those, the only unmapped keys expected are the `lat`/`lon` keys of a
`geo_point` (which is how a geo point is written in `_source`). A device document carries a couple of hundred `kismet.raw.*` leaf
fields; these are what Kibana Discover lists as "unmapped".
