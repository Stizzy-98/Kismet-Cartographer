"""Names of every Elasticsearch resource this project owns.

All of them start with `kismet-cartographer-`. The installer and the client both
refuse to touch anything that does not (see `esclient.require_owned`), because the
cluster-level template/pipeline privileges are not scoped by name.
"""

PREFIX = "kismet-cartographer-"
ECS_VERSION = "8.17.0"

# Write aliases (what the ingester writes to) -> concrete versioned indices.
INDEX_OBSERVATIONS = PREFIX + "observations"
INDEX_DEVICES = PREFIX + "devices"
INDEX_TRACK = PREFIX + "track"
INDEX_SYSTEM = PREFIX + "system"
READ_ALIAS_ALL = PREFIX + "all"
CONCRETE_VERSION = "v1"

FAMILIES = {
    INDEX_OBSERVATIONS: f"{INDEX_OBSERVATIONS}-{CONCRETE_VERSION}",
    INDEX_DEVICES: f"{INDEX_DEVICES}-{CONCRETE_VERSION}",
    INDEX_TRACK: f"{INDEX_TRACK}-{CONCRETE_VERSION}",
    INDEX_SYSTEM: f"{INDEX_SYSTEM}-{CONCRETE_VERSION}",
}

PIPELINE_NORMALIZE = PREFIX + "normalize-v1"
COMPONENT_SETTINGS = PREFIX + "settings-v1"
COMPONENT_COMMON = PREFIX + "mappings-common-v1"
COMPONENT_BY_FAMILY = {
    INDEX_OBSERVATIONS: PREFIX + "mappings-observations-v1",
    INDEX_DEVICES: PREFIX + "mappings-devices-v1",
    INDEX_TRACK: PREFIX + "mappings-track-v1",
    INDEX_SYSTEM: PREFIX + "mappings-system-v1",
}
INDEX_TEMPLATE_BY_FAMILY = {fam: fam for fam in FAMILIES}  # template name == alias name

# `kismet.record` value -> write alias
RECORD_INDEX = {
    "wifi.packet": INDEX_OBSERVATIONS,
    "adsb.frame": INDEX_OBSERVATIONS,
    "data.event": INDEX_OBSERVATIONS,      # any `data` row whose type is not ADSB (preserved, not dropped)
    "device": INDEX_DEVICES,
    "gps.snapshot": INDEX_TRACK,
    "snapshot.radiation": INDEX_TRACK,
    "snapshot.system": INDEX_SYSTEM,
    "alert": INDEX_SYSTEM,
    "message": INDEX_SYSTEM,
    "datasource": INDEX_SYSTEM,
    "ingest.run": INDEX_SYSTEM,
}

DATA_VIEW_TITLE = PREFIX + "*"
DATA_VIEW_NAME = "Kismet Cartographer"
DATA_VIEW_FALLBACK_ID = "kismet-cartographer-dataview"

ZERO_MAC = "00:00:00:00:00:00"

# ADS-B is line-of-sight (~450 km at cruise altitude, rarely more than ~700 km). An aircraft position farther
# than this from every point the collector itself recorded in the same capture is a decode error, not an aircraft.
MAX_EMITTER_RANGE_KM = 1000.0

# Kismet occasionally writes an absurd aircraft altitude (an identical 5.6e18 was seen on 38 documents). Civil ADS-B
# altitudes are metres in roughly this band; anything outside is a decode artefact and is not indexed as an altitude.
AIRCRAFT_ALTITUDE_RANGE_M = (-1500.0, 30000.0)


def index_for(record: str) -> str:
    """Write alias for a `kismet.record` value; unknown snapshot/other types go to the system index."""
    return RECORD_INDEX.get(record) or INDEX_SYSTEM
