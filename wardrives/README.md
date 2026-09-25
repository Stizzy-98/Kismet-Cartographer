# wardrives

Put your Kismet capture files (`*.kismet`) here, then run `./ingest` from the project folder.
You can install first and add files later; run `./ingest` again whenever you add more.

## Just want a WiGLE upload?

```bash
./wardrives/export_to_wigle.py
```

No Elasticsearch needed. Turns every `.kismet` file in this folder into `wigle_export.csv` right here,
ready to upload at <https://wigle.net/uploads>. Safe to run any time - the `.kismet` files are opened
read-only and never touched. See [docs/ingestion.md](../docs/ingestion.md#export-to-wigle-csv) for exactly
what ends up in the CSV.
