# Kibana: data view, map and dashboard

The Kibana layer is installed by `scripts/install_kibana.py` from the definitions in
`config/kibana/`. It is idempotent: a second run writes nothing.

```bash
python3 scripts/install_kibana.py --es-url ... --kibana-url ... --api-key-file ... --ca ... --dry-run
python3 scripts/install_kibana.py --es-url ... --kibana-url ... --api-key-file ... --ca ...
```

Exit codes: `0` ok, `1` failure, `2` usage/config, `3` cannot connect or authenticate.
Flags: the connection flags from `installation.md` (including `--space`), `--dry-run`, and `--force`
(rewrite every object even if up to date). Normally you just run `./install`, which calls this script.

## What is installed

| type | id | title |
|---|---|---|
| data view | resolved at install time | Kismet Cartographer (`kismet-cartographer-*`) |
| map | `kismet-cartographer-map` | Kismet Cartographer - Map |
| dashboard | `kismet-cartographer-overview` | Kismet Cartographer - Overview |
| search | `kismet-cartographer-recent-observations` | Kismet Cartographer - Recent observations |
| lens | `kismet-cartographer-lens-total-observations` | Total observations |
| lens | `kismet-cartographer-lens-unique-devices` | Unique devices |
| lens | `kismet-cartographer-lens-unique-bssids` | Unique BSSIDs |
| lens | `kismet-cartographer-lens-unique-ssids` | Unique SSIDs |
| lens | `kismet-cartographer-lens-aircraft-count` | Aircraft seen |
| lens | `kismet-cartographer-lens-observations-over-time` | Observations over time |
| lens | `kismet-cartographer-lens-rssi-distribution` | Signal strength distribution |
| lens | `kismet-cartographer-lens-observations-by-datasource` | Observations by datasource |
| lens | `kismet-cartographer-lens-observations-by-phy` | Observations by PHY |
| lens | `kismet-cartographer-lens-devices-by-type` | Devices by type |
| dashboard | `kismet-cartographer-hardware` | Kismet Cartographer - Devices & Hardware |
| lens | `kismet-cartographer-lens-hw-*` (10) | the panels of the hardware dashboard (see "The hardware dashboard") |

Every id starts with `kismet-cartographer-`; the installer refuses any definition that does
not, so it can never overwrite one of the unrelated dashboards in this Kibana.

Open: `<KIBANA_URL>/app/dashboards#/view/kismet-cartographer-overview`,
`<KIBANA_URL>/app/dashboards#/view/kismet-cartographer-hardware` and
`<KIBANA_URL>/app/maps/map/kismet-cartographer-map`.

## Source of truth: config/kibana/

* `config/kibana/data-view.json` - the data view spec (title, time field, field formats and
  labels) plus the fallback id.
* `config/kibana/saved-objects/*.json` - **one saved object per file**, each a plain JSON
  record with `id`, `type`, `attributes` and `references`. File name = id without the
  `kismet-cartographer-` prefix.

Two conventions make the files readable:

1. Kibana stores some attributes as JSON *strings* (`layerListJSON`, `mapStateJSON`,
   `uiStateJSON`, `panelsJSON`, `optionsJSON`, `searchSourceJSON`,
   `ignoreParentSettingsJSON`). In the files they are written as real JSON; the installer
   serialises any key ending in `JSON` on the way in.
2. References to the data view carry the placeholder id `kismet-cartographer-dataview`. The
   installer rewrites them to the id of the data view it actually resolved.

### Data view resolution

The installer looks for an existing data view whose **title** is `kismet-cartographer-*`
and reuses it whatever its id (in this deployment: `e0406831-558b-4211-9790-f141d10ff2fb`,
name "Kismet Cartographer"). Only when none exists does it create one with the id
`kismet-cartographer-dataview` from `data-view.json`. An existing, user-owned data view is
never renamed or rewritten.

### Writing method

Objects are written one at a time with
`POST /api/saved_objects/<type>/<id>?overwrite=true`, in the order search → lens → map →
dashboard, and only when the stored attributes/references differ from the definition.
The bulk `POST /api/saved_objects/_import` route is **not** used: on Kibana 9.4 it replays
the whole Lens migration chain over hand-written objects (which carry no
`typeMigrationVersion`) and answers HTTP 500. The per-object route stamps the current model
version itself.

## Reading `geo.location`

`geo.location` is the only geo_point every layer uses, but its **meaning** is in
`geo.location_type`:

| `geo.location_type` | meaning | where |
|---|---|---|
| `observer` | where the **collector** was | Wi-Fi packets, GPS track, alerts, messages, radiation pings, system snapshots |
| `observer_centroid` | Kismet's **average of collector positions** for a device - *not* where the transmitter is | device records |
| `emitter_reported` | the aircraft's **own** ADS-B position | `adsb.frame` and aircraft device records |

`geo.peak_signal_location` (device records) is the collector position where the strongest
signal was seen - still a collector position, not a transmitter position.

A Wi-Fi point on the map therefore says *"the collector was here and heard this"*, never
*"the access point is here"*. Only the aircraft layer shows self-reported emitter positions.

## The map and its layers

Bottom to top (the layer list shows them top-down). Position meaning is part of every layer name:

| # | layer | type | visible by default | what it shows |
|---|---|---|---|---|
| 1 | Road map (Elastic Maps Service) | basemap | yes | |
| 2 | Wi-Fi observation DENSITY - COLLECTOR positions (heat map) | `HEATMAP` (geo-grid, `heatmap`) | no | how many Wi-Fi packets were heard around each point; `kismet.record: wifi.packet` |
| 3 | Wi-Fi observations - COUNT per area (bubbles, COLLECTOR positions) | geo-grid clusters (`point`, count) | **yes** (zoom 0-13) | one circle per grid cell; **size, colour and label = number of packets** heard from that area, so counts can be compared at a glance. Cells get finer as you zoom in and hand over to the individual dots at zoom 12+ |
| 4 | Aircraft ADS-B frames - COUNT per area (bubbles, EMITTER-REPORTED positions) | geo-grid clusters | no | same idea for `geo.location_type: emitter_reported` |
| 5 | Aircraft - ADS-B REPORTED position | `MVT_VECTOR` | no | every aircraft frame/record as a small orange-red point |
| 6 | Wi-Fi observations - COLLECTOR position | `MVT_VECTOR` | **yes** (zoom 12+) | individual packets as **small white dots with a thin dark outline** (previously large pale dots that vanished on the light basemap) |
| 7 | Wi-Fi devices - OBSERVATION CENTROID, not the transmitter location | `MVT_VECTOR` | no | `kismet.record:"device" and geo.location_type:"observer_centroid"`, coloured by device type |
| 8 | Collector GPS track - COLLECTOR position | `MVT_VECTOR` | yes | blue points |
| 9 | Other geospatial events - COLLECTOR position | `MVT_VECTOR` | no | alerts, messages, radiation, system (messages are numerous and sit on the route) |
| 10 | Wi-Fi devices (icons) - OBSERVATION CENTROID, not the transmitter location | `MVT_VECTOR`, icon symbols | no (zoom 15+) | one map-pin icon per device centroid |
| 11 | Aircraft (icons) - ADS-B REPORTED position, one per aircraft | `MVT_VECTOR`, icon symbols | **yes** (zoom 11+) | one icon per aircraft device record at the position the aircraft reported |

Heat map and bubbles are **aggregations**: they answer "how much", not "where exactly". They
count **documents** (Wi-Fi packets), not distinct devices. They show where the **collector**
was, not where transmitters are, so a hot spot means "lots of traffic was heard while driving
here". Only the aircraft layers use emitter (self-reported) positions. Turn the heat map on and
the bubbles off (layer list) for a continuous density picture.

The dot and icon layers use **vector tiles** (`scalingType: MVT`, layer type `MVT_VECTOR`), which
is what makes millions of documents render: Elasticsearch renders each tile with `_mvt`
instead of shipping GeoJSON to the browser. The heat map and bubble layers use
Elasticsearch `geotile_grid` aggregations. All of them have `applyGlobalQuery` and
`applyGlobalTime` on, so the time picker, the KQL bar, the dashboard controls and map-bounds
filters all apply.

Tooltips: Wi-Fi layers show SSID/BSSID/MAC, source and destination MAC, RSSI, frequency,
channel, band, datasource, capture file and `geo.location_type`; the aircraft layer shows
ICAO, callsign, registration, type, owner, altitude, speed and heading; the track layer
shows GPS altitude/speed/heading/fix.

Saved view: the whole world (zoom 2), time `now-2y` → `now`. Zoom to your data, or use a layer's "Fit to data bounds" button.

### Known limitations

* **No geo_line track.** A proper line/track layer needs the Elasticsearch `geo_line`
  aggregation, which this cluster's **basic** licence rejects
  (`current license is non-compliant for [geo-line-agg]`). The GPS track is therefore a
  point layer; at the sampling rate of `gps.snapshot` it still draws as a line.
* **Aircraft use one colour, not an altitude ramp.** The layer was styled before the pipeline started
  dropping Kismet's absurd altitude artefact (an identical 5.6e18 on 38 documents); since schema version 4
  `aircraft.altitude` only holds values between -1,500 and 30,000 m (rejected values are kept in the
  stored-only `aircraft.rejected`), so an altitude ramp on `aircraft.altitude` can now be added in the layer
  style. The real altitude is in the tooltip.
* Layers are styled from vector-tile field metadata, so a colour ramp reflects the documents
  in the tiles currently on screen.

### Basemap

The Elastic Maps Service basemap is fetched by the **browser**, not by Kibana: the machine
viewing the dashboard must be able to reach `maps.elastic.co` / `tiles.maps.elastic.co`. If
it cannot, the layer shows "Unable to find EMS tile configuration ... Kibana is unable to
access Elastic Maps Service" and the map stays blank white - **the document layers still
render normally on that blank background**, the points are just harder to place. To work
offline, run the Elastic Maps Server container and point `map.emsUrl` at it, or add a
different tile source with "Add layer".

## The dashboard

`Kismet Cartographer - Overview` restores the time range `now-2y` → `now` when opened (widen
or narrow it to the dates you captured on); the time picker still works normally afterwards.

Controls (one filter bar across the top, all backed by the data view):
capture file (`kismet.source.file`), record type (`kismet.record`), PHY (`kismet.phy`),
datasource (`kismet.datasource.name`), SSID (`wifi.ssid`), BSSID (`wifi.bssid`), device MAC
(`wifi.mac`), Kismet device key (`kismet.device_key`), aircraft ICAO (`aircraft.icao`),
band (`wifi.band`), channel (`wifi.channel`) and a range slider on signal strength
(`wifi.rssi`).

Panels:

* five metrics - observations (`kismet.record : ("wifi.packet" or "adsb.frame")`, i.e. Wi-Fi
  packets + ADS-B frames only, never device or system records), unique devices (distinct
  `kismet.device_key` over `kismet.record : "device"`), unique BSSIDs, unique SSIDs, aircraft
  (distinct `aircraft.icao`);
* the map - full width, 38 grid rows tall (about two thirds of a screen);
* observations over time - date histogram split by `kismet.record`, full width directly below the map;
* signal strength distribution - histogram of `wifi.rssi`;
* observations by datasource - `kismet.datasource.name`;
* observations by PHY - `kismet.phy` (IEEE802.11 vs ADSB);
* devices by type - `kismet.device.type`;
* recent observations - the saved search, newest first, with columns record, PHY, SSID,
  BSSID, MAC, RSSI, channel, band, ICAO, callsign, datasource and capture file.

## The hardware dashboard

`Kismet Cartographer - Devices & Hardware` answers "who makes the Wi-Fi gear around me?". It is **Wi-Fi only**: the
dashboard carries the query `kismet.phy : "IEEE802.11"` (so the filter controls never list aircraft) and each
panel repeats it. Every panel reads device documents, so counts are devices, not packets. Panel titles start with the
question they answer - **Brand**, **Model**, **Device** or **Version** - and each dimension is shown once.
No chart has an "Other" bucket: the size limits are chosen to cover every value that exists, so nothing is hidden
or lumped together.

| Panel | Form | Fields |
|---|---|---|
| brands seen, models identified, devices with a known brand, APs offering WPA3 | four number tiles | `device.manufacturer`, `device.model.name`, `wifi.security` |
| Brand - top 15 brands | ranked bar | `device.manufacturer` |
| Model - brand to model | the dashboard's only treemap | `device.manufacturer`, `device.model.name` |
| Device - what kind of devices | waffle (1 square = 1%) | `kismet.device.type` |
| Version - Wi-Fi security generation | donut | `wifi.security` |
| Version - security of each brand's access points | 100% bars, one per brand, split by security version | `device.manufacturer`, `wifi.security` |
| Version - channel width by band | stacked bars | `wifi.channel_width`, `wifi.band` |

Controls: brand, model, device type, security version, band, capture file.

Where the values come from is in [data-model.md](data-model.md#hardware-brand-model-version). Models are only
known for devices that announce them (WPS), so the model treemap covers a minority of devices by design; the
"known brand" tile shows the brand coverage. Aircraft keep their own `device.*` fields in the index but are not shown here.

## Troubleshooting

**No points on the map.**

1. *Time range.* The captures are historical. Widen the picker (e.g. `now-2y` → `now`) or
   use an absolute range that covers the dates you captured on.
2. *Documents without a GPS fix have no `geo.location` at all* and can never be drawn. They
   are still indexed: `geo.status` is `no_fix` or `invalid` and `geo.reason` says why
   (Kismet writes 0/0 when there is no fix; the ingest pipeline drops such coordinates rather
   than inventing a position). Check with
   `kismet.record : "wifi.packet" and not geo.location : *` in Discover.
3. *Layer filters.* Each layer has its own KQL query; a dashboard control or query that
   contradicts it (for example record type = `device` while looking at the Wi-Fi packet
   layer) empties that layer.
4. *Blank white background but coloured points* - the EMS basemap is blocked (see above);
   that is a browser networking issue, not a data issue.
5. *Red exclamation mark on a layer* - open the layer's details in the layer list; it
   contains the tile error. An `AJAXError (401)` there means the browser session is not
   authenticated to `/internal/maps/mvt/getTile/...` (log in to Kibana again).

**Datasource, band, aircraft ICAO, PHY or device-type controls/panels show errors right after a load.**
The browser is holding a data view loaded before those fields existed (the data view was
originally created from a 23-field stub index). Hard-reload the tab (Ctrl+Shift+R) and, if
needed, Stack Management → Data views → Kismet Cartographer → **Refresh field list**.
`install_kibana.py` now also refreshes the field list server-side on every run
(`POST /api/data_views/data_view/<id>` with `refresh_fields: true`, a no-op for a current data view).

**A panel shows an error instead of a chart.** Check that the field still exists in the data
view (Stack Management → Data views → Kismet Cartographer → refresh the field list after new
indices appear), then re-run the installer.

**The installer says "refused: ... not under the 'kismet-cartographer-' namespace".** A
definition file declares an id outside the project namespace; nothing was written.

## Recreating the map by hand

If you would rather rebuild it in the UI: Maps → *Create map* → *Add layer* → *Documents* →
data view `kismet-cartographer-*`, geospatial field `geo.location`. For each layer set
*Scaling* to **Use vector tiles**, put the layer's KQL filter in the layer's own query box,
name it so the name states what the position means (see the table above), pick the tooltip
fields, and colour it by `wifi.rssi` / `kismet.device.type` / a fixed colour. Leave
*Apply global filter to layer data* and *Apply global time to layer data* on. Save as
"Kismet Cartographer - Map", then add it to a dashboard together with the Lens panels and a
control group over the fields listed above. The generated result is exactly what
`config/kibana/saved-objects/map.json` contains, so it is easier to edit that file and
re-run the installer.
