# Data model

Authoritative definitions: `config/elasticsearch/component-templates/kismet-cartographer-mappings-common-v1.json`
(mapping) and `kismet_cartographer/normalize.py` (how documents are built). ECS field names are used
where a natural equivalent exists (`@timestamp`, `event.*`, `source.mac`, `destination.mac`, `observer.*`,
`message`, `log.level`, `geo.location`); Kismet-specific information lives under `kismet.*`, `wifi.*`,
`aircraft.*` and `adsb.*` and is never renamed just to fit ECS.

## Indices

All have 1 primary shard, 0 replicas (configurable), `best_compression`, `dynamic: false`, and the default
ingest pipeline `kismet-cartographer-normalize-v1`. Each concrete index `X-v1` has a write alias `X`;
`kismet-cartographer-all` is a read alias over the four.

| Alias (write) / index | `kismet.record` values | Source table(s) | One document is... |
|---|---|---|---|
| `kismet-cartographer-observations` | `wifi.packet`, `adsb.frame`, `data.event` | `packets`, `data` | one packet row / one ADS-B frame / one `data` row of an unknown type |
| `kismet-cartographer-devices` | `device` | `devices` | one device per capture file (Wi-Fi AP/client/..., aircraft) |
| `kismet-cartographer-track` | `gps.snapshot`, `snapshot.radiation` | `snapshots` | one collector GPS fix / one position-only radiation ping |
| `kismet-cartographer-system` | `alert`, `message`, `snapshot.system`, `snapshot.<other>`, `datasource`, `ingest.run` | `alerts`, `messages`, `snapshots`, `datasources` | one alert / log line / system snapshot / datasource / per-file ingest ledger entry |

## Position: what `geo.location` means

`geo.location` is a `geo_point` and is present **only** when the source had a real position. Its meaning
is stated by `geo.location_type`:

| `geo.location_type` | Set on | Meaning | Is it the transmitter's location? |
|---|---|---|---|
| `observer` | Wi-Fi packets, GPS track, radiation pings, alerts, messages, system snapshots | Where the **collector** was when it recorded the row. | **No.** |
| `observer_centroid` | Wi-Fi/other device docs | Kismet's average of the collector positions at which the device was seen. | **No** - a rough "seen around here" marker, never an access point's coordinates. |
| `emitter_reported` | `adsb.frame`, aircraft `device` docs | The aircraft's **own** position as decoded from its ADS-B messages. | Yes - that is the aircraft's reported position. |
| `unspecified` | `data.event` (unknown `data` types) | A position was present but the meaning of the row type is unknown, so none is asserted. | Unknown |

For ADS-B frames the row's lat/lon is the aircraft's decoded position (typically tens to hundreds of km from the
receiver, with altitudes in the thousands of metres), so it is **not** the receiver's GPS. Kismet does not store a per-frame receiver position
for ADS-B.

Related fields:

| Field | Type | Meaning |
|---|---|---|
| `geo.status` | keyword | `ok`, `no_fix`, or `invalid`. Present on every document that has a position concept (not on `datasource`/`ingest.run`). |
| `geo.reason` | keyword | Why there is no location: `no_gps_fix` (Kismet wrote 0/0 or null), `out_of_range_or_not_numeric`, `centroid_outside_device_bounds`, `position_outside_device_bounds`, `implausible_range_from_collector`. |
| `geo.rejected` | object (stored, not indexed) | For `geo.status=invalid`: the lat/lon the source actually contained, so rejecting a value never erases it. Visible in Discover's JSON view / `_source`. |
| `geo.peak_signal_location` | geo_point | Device docs: collector position where the strongest signal was seen (also an observer position). |
| `gps.altitude`, `gps.speed`, `gps.heading`, `gps.fix`, `gps.name` | float/integer/keyword | The **collector's** GPS values (Wi-Fi packets, GPS track). Units are Kismet's (altitude m; speed and heading as recorded). Never set on aircraft documents. |
| `aircraft.altitude`, `.speed`, `.heading` | float | The aircraft's own reported values (altitude metres per Kismet; speed as recorded). |
| `kismet.device.bounds.{min,max}_{lat,lon}` | float | Device docs: Kismet's min/max box of recorded positions (only when real). |

**Rules.** A document without a real fix is still indexed (counts stay correct) but has no
`geo.location`, only `geo.status`/`geo.reason`. Nothing is interpolated, snapped, or copied from a neighbouring
row. Coordinates are stored as `{"lat": .., "lon": ..}` objects, never arrays, so ordering cannot be confused.

## Hardware: brand, model, version

Derived by `kismet_cartographer/hardware.py` from the stored device JSON so they can be charted; the originals
stay in `kismet.device.manufacturer` and `kismet.raw`. They exist on `device` documents only (packets do not carry them).
Every one that is set is listed in `kismet.derived`.

| Field | Meaning | Source |
|---|---|---|
| `device.manufacturer` | Brand, cleaned: `Vantiva USA LLC` and `Vantiva - Connected Home` -> `Vantiva`, corporate suffixes dropped, a few aliases merged (TP-Link, HP, HPE / Aruba, Cisco, ...). Absent when Kismet reports `Unknown`. | Kismet's OUI manufacturer; if unknown, the manufacturer the device announced in WPS. Aircraft: maker part of the ICAO model (`BOEING 737-8` -> `Boeing`). |
| `device.model.name` | Model name. | WPS model name (falls back to WPS model number); aircraft: ICAO type (`737-8`). Placeholder values (`12345`, `Model`, ...) are dropped. |
| `device.model.identifier` | Model number / revision, only when a model name exists. | WPS model number. Often a hardware/firmware revision such as `0.0.C`. |
| `wifi.security` | WPA generation of an **access point**: `Open`, `Enhanced Open (OWE)`, `WEP`, `WPA`, `WPA2`, `WPA2/WPA3` (transition), `WPA3`. | Kismet's `crypt` string. Not set for clients. |
| `wifi.auth` | `Open`, `Personal` (shared password) or `Enterprise` (802.1X). | same |
| `wifi.channel_width` | `20 MHz` ... `160 MHz`. Access points only. | advertised `ht_mode` |
| `wifi.wps.version` | `1.0` / `2.0`. | WPS version byte (`16` -> 1.0, `32` -> 2.0) |
| `wifi.wps.device_name` | The device's self-chosen WPS name (e.g. `Xfinity Wireless Gateway`). | WPS |

The WPS **serial number** is never read or stored outside `kismet.raw`; it identifies one physical unit.
Brand and model are as trustworthy as what the device announces: an AP can claim any WPS model.

Adding these fields did not bump `SCHEMA_VERSION`: only device documents changed, and they were refreshed in place with
`python3 scripts/ingest_kismet.py <dir> --tables devices` (partial runs write no completion marker, so
`--skip-complete` is unaffected). New captures get the fields automatically.

## Kismet sentinel / quality rules

Problems Kismet's own logs are known to contain, and what the pipeline does about each. "How common" is a rough guide.

| Source value | How common | Handling |
|---|---|---|
| lat = lon = 0.0 on packets/frames/alerts/messages | very common (any time the GPS has no fix) | `geo.status=no_fix`; no `geo.location`. (`(0,0)` together only; a lone 0 is a valid coordinate.) |
| ADS-B device location `[0, 0]` | common (aircraft heard without a position message) | no position (`no_fix`); the aircraft is still indexed with its ICAO, callsign, altitude, ... |
| `signal = 0` on packets | common | No `wifi.rssi`; the original is kept in `kismet.packet.signal_raw`. 0 dBm is not a real received power. |
| Device `avg_loc` outside the device's own min/max box | rare | An average cannot lie outside the points it averages; the centroid is rejected (`geo.reason=centroid_outside_device_bounds`), the valid `peak_signal_location` is kept. Kismet wrote values such as (0, 90), (90, 90). |
| Timestamp <= 0 or after year 2100 | not expected | Document kept without `@timestamp`, warning counted; a time is never invented. |
| `packets.devkey = 0` | all rows | Not usable as a device link; the link is derived through the MAC (below). |
| ADS-B decode errors: impossible latitude (e.g. 112.2 deg) | rare | `geo.status=invalid`, no `geo.location`, source value kept in `geo.rejected`. |
| ADS-B position in valid range but physically impossible (e.g. a point thousands of km from where the receiver ever was) | rare | ADS-B is line-of-sight (a few hundred km). A position more than 1,000 km from every position the collector itself recorded in the same capture is a decode error: `geo.reason=implausible_range_from_collector`, no `geo.location`, source value kept in `geo.rejected`. Limit: `MAX_EMITTER_RANGE_KM` in `kismet_cartographer/constants.py`. A capture with no GPS fix has nothing to compare against and is not filtered. |
| Aircraft altitude outside -1,500..30,000 m (an identical `5.622567663155806e18` was seen) | rare | `aircraft.altitude` is not indexed; the position, if real, is kept; the value is preserved in the stored-only `aircraft.rejected.altitude`. Band: `AIRCRAFT_ALTITUDE_RANGE_M` in `constants.py`. |

## Derived fields

Everything not read directly from a source column/JSON key is listed in `kismet.derived` on that document.

| Derived field | Computed from | Where |
|---|---|---|
| `wifi.band`, `wifi.channel` (packets) | frequency via the standard 802.11 frequency plan | packets; band also on devices |
| `kismet.device_key`, `kismet.device.type` (packets) | the device row in the **same file** whose MAC equals `source.mac` | packets |
| `wifi.bssid`, `wifi.ssid` (packets) | the AP device (same file) that is the packet's source, or its destination | packets |
| `kismet.device_key`, `aircraft.icao` (ADS-B frames) | the aircraft device (same file) with the same Kismet `devmac` | adsb frames |
| `@timestamp` (datasource docs) | the capture's first timestamp (a datasource row has no time of its own) | datasources |

A packet that is not linkable (no matching device in its file, or a hidden SSID) simply lacks those fields.

## Provenance (every document)

| Field | Content |
|---|---|
| `kismet.source.file` | source file name (no path) - filter a map to one capture |
| `kismet.source.file_hash` | SHA-256 of the file content |
| `kismet.source.table`, `kismet.source.rowid` | the exact source row (rowid pointer, also for the raw packet bytes that are not copied) |
| `kismet.version`, `kismet.db_version` | from the file's `KISMET` table (for example 2025.09.0 / 9) |
| `kismet.record` | original record type (table -> `wifi.packet`, `device`, ...) |
| `kismet.device_key` | Kismet device key (devices; packets/frames when derived) |
| `kismet.datasource.{uuid,name,type,interface,hardware}` | which capture source saw it (denormalised from the file's `datasources` table) |
| `kismet.ingest.{run_id,schema_version,tool_version}` | which run/version wrote it |
| `event.ingested` | added by the ingest pipeline |
| `kismet.raw` | verbatim original JSON (device, alert, datasource, snapshot, unknown-data rows): stored, **not indexed** |
| `kismet.parse_error` | set when the source JSON could not be parsed (the document is still indexed from the row's columns) |

## Field reference (selected)

`wifi.*` - `mac`, `bssid`, `ssid` (first advertised), `ssids[]`, `probed_ssids[]`, `ssid_hidden`, `channel` (keyword,
as Kismet reports for devices), `frequency` (**MHz**; Kismet stores kHz, original in `kismet.packet.frequency_khz`),
`band`, `rssi` (dBm, float), `phy`, `crypt`, `transmitter_mac`, `num_associated_clients`.
`kismet.packet.*` - `len`, `full_len`, `hash`, `packet_id`, `dlt`, `error`, `tags[]`, `datarate`, `signal_raw`,
`frequency_khz`, `devkey_raw`. `kismet.device.*` - `type`, `first_seen`, `last_seen`, `name`, `commonname`,
`manufacturer`, `strongest_signal`, `bytes_data`, `packets_total`, `bounds`. `aircraft.*` - `icao`, `callsign`,
`registration`, `type`, `model`, `owner`, `category`, `altitude`, `speed`, `heading`. `adsb.frame` - the raw Mode-S
message string exactly as Kismet stored it (`*8da...;`); it is not decoded.
`kismet.alert.*` - `header`, `class`, `severity`, `hash`, `phy_id`, `channel`, `device_key`.

Timestamps: `@timestamp` = ISO-8601 UTC, millisecond precision, from `ts_sec`/`ts_usec` (devices: `last_time`, with
`event.start`/`event.end` = first/last seen). `kismet.ts.{sec,usec}` keep the exact source values.

## Document IDs and idempotence

`_id = base64url( sha256( <file sha256> | <table> | <row key> )[:15 bytes] )` (20 characters). Row key: `rowid` for
packets/data/alerts/messages/snapshots, `devkey` for devices, `uuid` for datasources. The same file content always
yields the same ids, so re-ingesting overwrites rather than duplicates (`ingestion.md`). One marker per file
(`kismet.record: ingest.run`) stores per-table row/doc/no-GPS/parse-error counts and the run status.

## Example queries

```
GET kismet-cartographer-all/_field_caps?fields=geo.location            # must report geo_point only
GET kismet-cartographer-observations/_search
{ "query": { "bool": { "filter": [
    { "term": { "kismet.record": "wifi.packet" } },
    { "term": { "kismet.source.file": "Kismet-YYYYMMDD-HH-MM-SS-1.kismet" } },
    { "geo_distance": { "distance": "1km", "geo.location": { "lat": 40.0, "lon": -100.0 } } } ] } } }

GET kismet-cartographer-all/_search      # collector positions that had no fix, by reason
{ "size": 0, "aggs": { "r": { "terms": { "field": "geo.reason" } } } }

GET kismet-cartographer-devices/_search  # strongest APs for an SSID
{ "query": { "term": { "wifi.ssid": "MyNetwork" } }, "sort": [ { "kismet.device.strongest_signal": "desc" } ] }

GET kismet-cartographer-all/_search      # distinct aircraft
{ "size": 0, "aggs": { "planes": { "cardinality": { "field": "aircraft.icao" } } } }

GET kismet-cartographer-track/_search    # the collector's route, in time order
{ "query": { "term": { "kismet.record": "gps.snapshot" } }, "sort": [ "@timestamp" ], "size": 1000 }
```
