# Testing

## Unit tests (no Elasticsearch, no captures of your own)

```bash
python3 -m unittest discover -s tests
```

About forty tests run in well under a second and need nothing but Python. The pipeline tests run on a small **invented** capture
that `kismet_cartographer/synthetic.py` builds on the fly (made-up brands, networks, aircraft and route; nothing from a real capture).
They check that:

* every source row becomes exactly one document, with deterministic ids and full provenance (file, hash, table, row);
* positions are never swapped, never `(0,0)`, and never invented: no-fix rows have no coordinates, impossible ones are rejected
  with the original value kept;
* Kismet's known bad data is handled: signal 0, garbage device centroids, impossible ADS-B positions and altitudes, malformed JSON;
* brand, model, security generation and channel width are derived correctly, and the WPS serial number is never indexed;
* the connection flags, the API-key forms, the `--host` shortcut, saved settings and capture listing behave;
* the original file is never modified.

The tests are isolated from the machine they run on: they ignore your environment, your saved settings and your credentials.

## Try the whole thing without captures of your own

```bash
python3 scripts/make_sample_capture.py            # writes wardrives/sample-capture.kismet (invented data)
./ingest
```

This loads the same invented capture into your stack, which is a quick way to see the dashboards fill. Remove the file (and see
[operations.md](operations.md) to remove its documents) when you are done, so it does not mix with real data.

## Check a real load

```bash
./install ... --check                 # connection, certificate, key permissions
./ingest --validate                   # loads wardrives/, then checks the stored data against your source files
python3 scripts/validate_elastic.py --source wardrives/ --sample 300     # same check on its own, more samples
python3 tests/verify_rejected_records.py wardrives/                      # every rejected position/altitude against its source row
```

`validate_elastic.py` checks mappings, that no document has a fake `(0,0)` point, that counts per file match what was loaded, and that a
random sample of stored documents equals the original rows (coordinates, time, signal, frequency).
