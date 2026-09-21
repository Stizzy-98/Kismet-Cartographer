"""Kismet Cartographer: KismetDB -> Elasticsearch -> Kibana geospatial pipeline.

Standard library only (Python >= 3.9). See docs/architecture.md.
"""

__version__ = "1.0.0"

# Bump whenever the shape of an indexed document changes. It is stored in every
# document and in the per-file ingest marker, so `--skip-complete` never skips a
# file that was ingested with an older document shape.
SCHEMA_VERSION = 4
