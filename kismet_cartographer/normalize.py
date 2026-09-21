"""KismetDB rows -> normalized Elasticsearch documents.

Design rules (see docs/data-model.md):
  * Nothing is invented. Every field comes from a column/JSON key of the source row, or is
    listed in `kismet.derived` when it was computed from other source data (channel from
    frequency, SSID/BSSID looked up from a device seen in the same file, ...).
  * (0, 0) is "no fix". Such documents are still indexed (counts stay right) but get no
    geo.location, only geo.status = "no_fix".
  * Position semantics are explicit in `geo.location_type`:
        observer           where the collector was (Wi-Fi packets, GPS track, alerts, messages)
        observer_centroid  Kismet's average of collector positions for a device (NOT the
                           transmitter's location)
        emitter_reported   the aircraft's own ADS-B position
  * The original Kismet JSON is kept verbatim under `kismet.raw` (stored, not indexed).
  * Values that are Kismet sentinels are mapped to "absent" in the normalized field while the
    original is preserved (e.g. signal 0 -> no wifi.rssi, kismet.packet.signal_raw = 0).
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple

from . import SCHEMA_VERSION, __version__
from .constants import AIRCRAFT_ALTITUDE_RANGE_M, ECS_VERSION, MAX_EMITTER_RANGE_KM, ZERO_MAC
from .geo import NO_FIX, OK, INVALID, classify, from_kismet_geopoint, km_to_box, num
from . import hardware
from .kismetdb import KismetDb, blob_json

AP_TYPES = {"Wi-Fi AP", "Wi-Fi WDS AP"}
_TAG_SPLIT = re.compile(r"[,;\s]+")

# Columns read per table (rowid is always first in the yielded tuple).
PACKET_COLS = ["ts_sec", "ts_usec", "phyname", "sourcemac", "destmac", "transmac", "frequency", "devkey",
               "lat", "lon", "alt", "speed", "heading", "packet_len", "signal", "datasource", "dlt", "error",
               "tags", "datarate", "hash", "packetid", "packet_full_len"]
DEVICE_COLS = ["first_time", "last_time", "devkey", "phyname", "devmac", "strongest_signal", "min_lat", "min_lon",
               "max_lat", "max_lon", "avg_lat", "avg_lon", "bytes_data", "type", "device"]
DATA_COLS = ["ts_sec", "ts_usec", "phyname", "devmac", "lat", "lon", "alt", "speed", "heading", "datasource",
             "type", "json"]
ALERT_COLS = ["ts_sec", "ts_usec", "phyname", "devmac", "lat", "lon", "header", "json"]
MESSAGE_COLS = ["ts_sec", "lat", "lon", "msgtype", "message"]
SNAPSHOT_COLS = ["ts_sec", "ts_usec", "lat", "lon", "snaptype", "json"]
DATASOURCE_COLS = ["uuid", "typestring", "definition", "name", "interface", "json"]


class DeviceRef(NamedTuple):
    """What we remember about a device so packet/frame rows can be linked to it (same file only)."""
    devkey: str
    type: Optional[str]
    ssid: Optional[str]
    is_ap: bool
    icao: Optional[str]
    callsign: Optional[str]
    registration: Optional[str]


class TableStats:
    """Per-table counters + malformed-record reporting."""

    def __init__(self, table: str, sink: Optional[Callable[[str, Any, str], None]] = None):
        self.table = table
        self.rows = 0
        self.docs = 0
        self.geo_ok = 0
        self.no_gps = 0
        self.invalid_gps = 0
        self.parse_errors = 0
        self.warnings: Dict[str, int] = {}
        self.examples: List[str] = []
        self._sink = sink

    def reject(self, rowid: Any, msg: str) -> None:
        self.parse_errors += 1
        if len(self.examples) < 5:
            self.examples.append(f"rowid {rowid}: {msg}")
        if self._sink:
            self._sink(self.table, rowid, msg)

    def warn(self, kind: str) -> None:
        self.warnings[kind] = self.warnings.get(kind, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        return {"rows": self.rows, "docs": self.docs, "geo_ok": self.geo_ok, "no_gps": self.no_gps,
                "invalid_gps": self.invalid_gps, "parse_errors": self.parse_errors,
                "warnings": dict(self.warnings)}


class FileContext:
    def __init__(self, db: KismetDb, sha256: str, run_id: str):
        self.db = db
        self.name = db.name
        self.sha256 = sha256
        self.run_id = run_id
        self.kismet_version = db.kismet_version
        self.db_version = db.db_version
        self.datasources: Dict[str, Dict[str, Any]] = {}
        self.devices: Dict[Tuple[str, str], DeviceRef] = {}
        self.first_ts, self.last_ts = db.time_bounds()
        self._prefix = (sha256 + "|").encode()
        self.max_emitter_km = MAX_EMITTER_RANGE_KM
        self._box: Any = "unset"

    def implausible_emitter(self, lat: float, lon: float) -> bool:
        """True if an aircraft position is farther than the ADS-B range limit from everywhere the collector
        was in this capture. Without a collector fix there is nothing to compare with: accept it."""
        if not self.max_emitter_km:
            return False
        if self._box == "unset":
            self._box = self.db.collector_bbox()
        return self._box is not None and km_to_box(lat, lon, self._box) > self.max_emitter_km

    def doc_id(self, table: str, key: Any) -> str:
        """Deterministic id: same file content + same source row => same _id => idempotent re-ingest."""
        h = hashlib.sha256(self._prefix + table.encode() + b"|" + str(key).encode()).digest()[:15]
        return base64.urlsafe_b64encode(h).decode()


# --------------------------------------------------------------------------- helpers
_iso_cache: Dict[int, str] = {}


def iso(sec: Any, usec: Any = 0) -> Optional[str]:
    """Epoch seconds (+ microseconds) -> ISO-8601 UTC with millisecond precision, or None if implausible."""
    if isinstance(sec, float):
        sec = int(sec)
    if not isinstance(sec, int) or sec <= 0 or sec > 4102444800:
        return None
    base = _iso_cache.get(sec)
    if base is None:
        base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec))
        if len(_iso_cache) > 8192:
            _iso_cache.clear()
        _iso_cache[sec] = base
    ms = 0
    if isinstance(usec, int) and 0 <= usec < 1_000_000:
        ms = usec // 1000
    return f"{base}.{ms:03d}Z"


def compact(d: Dict[str, Any]) -> Dict[str, Any]:
    for k in [k for k, v in d.items() if v is None or v == "" or v == [] or v == {}]:
        del d[k]
    return d


def band_channel(mhz: Optional[float]) -> Tuple[Optional[str], Optional[int]]:
    """Standard 802.11 frequency plan. Derived (Kismet does not store a channel per packet)."""
    if not mhz or mhz <= 0:
        return None, None
    f = int(round(mhz))
    if 2400 <= f <= 2500:
        if f == 2484:
            return "2.4GHz", 14
        return "2.4GHz", (f - 2407) // 5 if (f - 2407) % 5 == 0 else None
    if 5150 <= f <= 5895:
        return "5GHz", (f - 5000) // 5 if (f - 5000) % 5 == 0 else None
    if 5925 <= f <= 7125:
        return "6GHz", (f - 5950) // 5 if (f - 5950) % 5 == 0 else None
    return None, None


def _inside_bounds(lat: float, lon: float, b: Tuple[float, float, float, float], tol: float = 1e-3) -> bool:
    """Is (lat, lon) inside the device's own min/max box? A mean/last position must be."""
    mnl, mnn, mxl, mxn = b
    return (min(mnl, mxl) - tol <= lat <= max(mnl, mxl) + tol) and (min(mnn, mxn) - tol <= lon <= max(mnn, mxn) + tol)


def _altitude(v: Any, stats: "TableStats") -> Tuple[Optional[float], Optional[float]]:
    """(altitude, rejected). An aircraft altitude outside the plausible band is not indexed; the value is returned
    as `rejected` so the caller can keep it in the stored-only aircraft.rejected object."""
    f = num(v)
    if f is None:
        return None, None
    lo, hi = AIRCRAFT_ALTITUDE_RANGE_M
    if lo <= f <= hi:
        return f, None
    stats.warn("aircraft_altitude_out_of_range")
    return None, f


def _zero_is_none(v: Any) -> Optional[float]:
    """Kismet stores 0 when a signal value is absent; 0 dBm is not a real received power."""
    f = num(v)
    return None if f is None or f == 0.0 else f


def _base(ctx: FileContext, record: str, table: str, rowid: Any, dataset: str, kind: str = "event",
          category: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "ecs": {"version": ECS_VERSION},
        "event": {"kind": kind, "module": "kismet", "dataset": dataset,
                  "category": category or ["network"], "type": ["info"]},
        "kismet": {
            "record": record, "version": ctx.kismet_version, "db_version": ctx.db_version,
            "source": {"file": ctx.name, "file_hash": ctx.sha256, "table": table, "rowid": rowid},
            "ingest": {"run_id": ctx.run_id, "schema_version": SCHEMA_VERSION, "tool_version": __version__},
        },
    }


def _apply_ts(doc: Dict[str, Any], ts: Optional[str], stats: TableStats) -> None:
    if ts:
        doc["@timestamp"] = ts
    else:
        stats.warn("bad_timestamp")  # never fabricate a time; the doc is kept without @timestamp


_DEFAULT_REASON = {NO_FIX: "no_gps_fix", INVALID: "out_of_range_or_not_numeric"}


def _apply_geo(doc: Dict[str, Any], status: str, lat: Optional[float], lon: Optional[float],
               loc_type: str, stats: TableStats, reason: Optional[str] = None,
               raw: Optional[Tuple[Any, Any]] = None) -> bool:
    geo: Dict[str, Any] = {"status": status}
    if status != OK:
        geo["reason"] = reason or _DEFAULT_REASON.get(status, status)
    if status == INVALID and raw is not None:
        # Keep what the source said (it is not indexed, only stored): rejecting a value must not erase it.
        rej = compact({"lat": num(raw[0]), "lon": num(raw[1])})
        if rej:
            geo["rejected"] = rej
    if status == OK:
        geo["location"] = {"lat": lat, "lon": lon}
        geo["location_type"] = loc_type
        stats.geo_ok += 1
    elif status == NO_FIX:
        stats.no_gps += 1
    else:
        stats.invalid_gps += 1
    doc["geo"] = geo
    return status == OK


def _ds_fields(ctx: FileContext, uuid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not uuid:
        return None
    ds = ctx.datasources.get(uuid, {})
    return compact({"uuid": uuid, "name": ds.get("name"), "type": ds.get("type"), "interface": ds.get("interface"),
                    "hardware": ds.get("hardware")})


# --------------------------------------------------------------------------- datasources
def datasource_docs(ctx: FileContext, stats: TableStats) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Also fills ctx.datasources, which every later table uses to denormalize datasource name/type."""
    for r in ctx.db.select("datasources", DATASOURCE_COLS):
        rowid, uuid, typestring, definition, name, interface, blob = r
        stats.rows += 1
        obj, err = blob_json(blob)
        if err and err != "null":
            stats.reject(rowid, f"datasource json: {err}")
        obj = obj if isinstance(obj, dict) else {}
        info = {"name": name, "type": typestring, "interface": interface,
                "hardware": obj.get("kismet.datasource.hardware")}
        if uuid:
            ctx.datasources[uuid] = info
        doc = _base(ctx, "datasource", "datasources", rowid, "kismet.datasource", kind="state", category=["configuration"])
        # A datasource row has no time of its own; anchor it to the capture start (declared as derived).
        ts = iso(ctx.first_ts)
        _apply_ts(doc, ts, stats)
        d = compact({"uuid": uuid, "name": name, "type": typestring, "interface": interface,
                     "definition": definition, "hardware": info["hardware"]})
        doc["kismet"]["datasource"] = d
        doc["observer"] = compact({"name": name, "type": typestring})
        doc["kismet"]["derived"] = ["@timestamp"]
        if obj:
            doc["kismet"]["raw"] = obj
        stats.docs += 1
        yield "datasource", ctx.doc_id("datasources", uuid or rowid), doc


# --------------------------------------------------------------------------- devices
def _uniq(seq: List[Optional[str]]) -> List[str]:
    seen, out = set(), []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _records(v: Any) -> List[Dict[str, Any]]:
    if isinstance(v, dict):
        v = list(v.values())
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def device_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
                ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Also fills ctx.devices (used to link packets/frames to devices within the same file)."""
    for r in ctx.db.select("devices", DEVICE_COLS, limit=limit):
        (rowid, first_time, last_time, devkey, phy, devmac, strongest, min_lat, min_lon, max_lat, max_lon,
         avg_lat, avg_lon, bytes_data, dtype, blob) = r
        stats.rows += 1
        try:
            doc, ref = _build_device(ctx, stats, rowid, first_time, last_time, devkey, phy, devmac, strongest,
                                     (min_lat, min_lon, max_lat, max_lon), (avg_lat, avg_lon), bytes_data, dtype, blob)
        except Exception as e:  # one bad row must not abort the file
            stats.reject(rowid, f"device build failed: {type(e).__name__}: {e}")
            continue
        if phy and devmac and ref:
            ctx.devices[(phy, str(devmac).upper())] = ref
        stats.docs += 1
        yield "device", ctx.doc_id("devices", devkey or f"row{rowid}"), doc


def _build_device(ctx, stats, rowid, first_time, last_time, devkey, phy, devmac, strongest, bounds, avg, bytes_data,
                  dtype, blob) -> Tuple[Dict[str, Any], DeviceRef]:
    obj, err = blob_json(blob)
    parse_error = None
    if err:
        parse_error = err
        stats.reject(rowid, f"device json: {err}")
        obj = {}
    if not isinstance(obj, dict):
        stats.reject(rowid, "device json is not an object")
        parse_error, obj = "not an object", {}

    is_air = phy == "ADSB"
    doc = _base(ctx, "device", "devices", rowid, "kismet.device", kind="state")
    _apply_ts(doc, iso(last_time) or iso(first_time), stats)
    ev = doc["event"]
    ev["start"], ev["end"] = iso(first_time), iso(last_time)
    compact(ev)
    k = doc["kismet"]
    if parse_error:
        k["parse_error"] = parse_error[:300]
    mac = str(devmac).upper() if devmac else None
    k.update(compact({"phy": phy, "devmac": mac, "device_key": devkey}))

    p = "kismet.device.base."
    sig = obj.get(p + "signal") if isinstance(obj.get(p + "signal"), dict) else {}
    loc = obj.get(p + "location") if isinstance(obj.get(p + "location"), dict) else {}
    dev: Dict[str, Any] = {
        "type": dtype,
        "first_seen": iso(first_time), "last_seen": iso(last_time),
        "name": obj.get(p + "name") or None,
        "commonname": obj.get(p + "commonname") or None,
        "manufacturer": obj.get(p + "manufacturer") or obj.get(p + "manuf") or None,
        "strongest_signal": _zero_is_none(strongest),
        "bytes_data": bytes_data,
        "packets_total": obj.get(p + "packets.total"),
    }
    # Bounding box of positions Kismet recorded for this device (observer positions for Wi-Fi,
    # the aircraft's own track for ADS-B). Only when at least one corner is a real coordinate.
    mnl, mnn, mxl, mxn = bounds
    have_bounds = classify(mnl, mnn)[0] == OK and classify(mxl, mxn)[0] == OK
    if have_bounds:
        dev["bounds"] = {"min_lat": mnl, "min_lon": mnn, "max_lat": mxl, "max_lon": mxn}
    k["device"] = compact(dev)

    # datasources that saw this device
    uuids = [s.get("kismet.common.seenby.uuid") for s in _records(obj.get(p + "seenby"))]
    uuids = _uniq(uuids)
    if uuids:
        k["datasource"] = {"uuid": uuids,
                           "name": _uniq([ctx.datasources.get(u, {}).get("name") for u in uuids]),
                           "type": _uniq([ctx.datasources.get(u, {}).get("type") for u in uuids])}
        compact(k["datasource"])
        obs = compact({"name": (k["datasource"].get("name") or [None])[0],
                       "type": (k["datasource"].get("type") or [None])[0]})
        if obs:
            doc["observer"] = obs

    derived: List[str] = []
    icao = callsign = reg = ssid = None
    ap = dtype in AP_TYPES

    if is_air:
        adsb = obj.get("adsb.device") if isinstance(obj.get("adsb.device"), dict) else {}
        rec = adsb.get("kismet.adsb.icao_record") if isinstance(adsb.get("kismet.adsb.icao_record"), dict) else {}
        icao = adsb.get("adsb.device.icao") or None
        callsign = (adsb.get("adsb.device.callsign") or "").strip() or None
        reg = rec.get("adsb.icao.regid") or None
        last = loc.get("kismet.common.location.last") if isinstance(loc.get("kismet.common.location.last"), dict) else {}
        status, la, lo = from_kismet_geopoint(last.get("kismet.common.location.geopoint"))
        reason = None
        raw_pt = (la, lo) if status == OK else None
        if status == OK and have_bounds and not _inside_bounds(la, lo, bounds):
            status, la, lo, reason = INVALID, None, None, "position_outside_device_bounds"
        elif status == OK and ctx.implausible_emitter(la, lo):
            status, la, lo, reason = INVALID, None, None, "implausible_range_from_collector"
        _apply_geo(doc, status, la, lo, "emitter_reported", stats, reason, raw=raw_pt)
        alt, alt_rejected = _altitude(last.get("kismet.common.location.alt"), stats)
        doc["aircraft"] = compact({
            "icao": icao, "callsign": callsign, "registration": reg,
            "type": rec.get("adsb.icao.type"), "model": rec.get("adsb.icao.model"),
            "owner": rec.get("adsb.icao.owner"), "category": rec.get("adsb.icao.atype"),
            # Only meaningful when the position is real; the altitude beside a [0,0] fix is still
            # reported by the aircraft, so it is kept independently of geo.status.
            "altitude": alt,
            "speed": num(last.get("kismet.common.location.speed")),
            "heading": num(last.get("kismet.common.location.heading")),
        })
        if alt_rejected is not None:
            doc["aircraft"]["rejected"] = {"altitude": alt_rejected}
        a_brand, a_model = hardware.aircraft_identity(rec.get("adsb.icao.model"), rec.get("adsb.icao.type"))
        if a_brand or a_model:
            doc["device"] = compact({"manufacturer": a_brand, "model": compact({"name": a_model})})
            derived += ["device.manufacturer", "device.model.name"]
    else:
        avg_loc = loc.get("kismet.common.location.avg_loc") if isinstance(loc.get("kismet.common.location.avg_loc"), dict) else {}
        status, la, lo = from_kismet_geopoint(avg_loc.get("kismet.common.location.geopoint"))
        if status != OK:  # fall back to the table's own avg_lat/avg_lon columns
            s2, la2, lo2 = classify(*avg)
            if s2 == OK:
                status, la, lo = s2, la2, lo2
        reason = None
        # Kismet occasionally writes a garbage avg_loc such as (0, 90) or (90, 90) for a device seen at a
        # single point (rare). A mean of positions cannot
        # lie outside the device's own min/max box, so such a centroid is rejected, not plotted.
        raw_pt = (la, lo) if status == OK else None
        if status == OK and have_bounds and not _inside_bounds(la, lo, bounds):
            status, la, lo, reason = INVALID, None, None, "centroid_outside_device_bounds"
        _apply_geo(doc, status, la, lo, "observer_centroid", stats, reason, raw=raw_pt)
        ps, pla, plo = from_kismet_geopoint(
            (sig.get("kismet.common.signal.peak_loc") or {}).get("kismet.common.location.geopoint")
            if isinstance(sig.get("kismet.common.signal.peak_loc"), dict) else None)
        if ps == OK:
            doc["geo"]["peak_signal_location"] = {"lat": pla, "lon": plo}

        if phy == "IEEE802.11":
            d11 = obj.get("dot11.device") if isinstance(obj.get("dot11.device"), dict) else {}
            adv = _records(d11.get("dot11.device.advertised_ssid_map"))
            ssids = _uniq([a.get("dot11.advertisedssid.ssid") for a in adv])
            probed = _uniq([a.get("dot11.probedssid.ssid") for a in _records(d11.get("dot11.device.probed_ssid_map"))])
            ssid = ssids[0] if ssids else None
            last_bssid = d11.get("dot11.device.last_bssid")
            bssid = mac if ap else (last_bssid if last_bssid and last_bssid != ZERO_MAC else None)
            khz = num(obj.get(p + "frequency"))
            mhz = khz / 1000.0 if khz and khz > 0 else None
            band, chan_calc = band_channel(mhz)
            chan = obj.get(p + "channel")
            wifi = {
                "phy": phy, "mac": mac, "bssid": bssid, "ssid": ssid, "ssids": ssids, "probed_ssids": probed,
                "ssid_hidden": True if (ap and adv and not ssids) else None,
                "channel": str(chan) if chan not in (None, "", 0, "0") else None,
                "frequency": mhz, "band": band, "crypt": obj.get(p + "crypt") or None,
                "rssi": _zero_is_none(sig.get("kismet.common.signal.last_signal")),
                "num_associated_clients": d11.get("dot11.device.num_associated_clients"),
            }
            if band:
                derived.append("wifi.band")
            wps = hardware.wps_identity(d11)
            if wps.get("version") or wps.get("device_name"):
                wifi["wps"] = compact({"version": wps.get("version"), "device_name": wps.get("device_name")})
            if ap:  # the crypt / width of a client is not a property of the client
                gen, auth = hardware.wifi_security(wifi["crypt"])
                width = hardware.channel_width(hardware.ht_mode_of(d11))
                wifi.update({"security": gen, "auth": auth, "channel_width": width})
                derived += [f for f, v in (("wifi.security", gen), ("wifi.auth", auth), ("wifi.channel_width", width)) if v]
            doc["wifi"] = compact(wifi)
            doc["source"] = compact({"mac": mac})
            # brand: the OUI manufacturer Kismet resolved, else the manufacturer the device announced in WPS
            brand = hardware.clean_brand(dev.get("manufacturer")) or hardware.clean_brand(wps.get("manufacturer"))
            model = hardware.clean_model(wps.get("model_name")) or hardware.clean_model(wps.get("model_number"))
            ident = hardware.clean_model(wps.get("model_number")) if model and wps.get("model_name") else None
            if brand or model:
                doc["device"] = compact({"manufacturer": brand, "model": compact({"name": model, "identifier": ident})})
                derived += [f for f, v in (("device.manufacturer", brand), ("device.model.name", model),
                                           ("device.model.identifier", ident)) if v]
    if derived:
        k["derived"] = derived
    if obj:
        k["raw"] = obj  # verbatim original device JSON: stored, not indexed
    return doc, DeviceRef(devkey or "", dtype, ssid, ap, icao, callsign, reg)


# --------------------------------------------------------------------------- packets
def packet_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
                ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    devices = ctx.devices
    datasources = ctx.datasources
    for r in ctx.db.select("packets", PACKET_COLS, limit=limit):
        (rowid, ts_sec, ts_usec, phy, src, dst, trans, freq, devkey_raw, lat, lon, alt, speed, heading, plen, sig,
         dsu, dlt, err, tags, rate, phash, pid, pfull) = r
        stats.rows += 1
        try:
            doc = _base(ctx, "wifi.packet", "packets", rowid, "kismet.wifi.packet")
            _apply_ts(doc, iso(ts_sec, ts_usec), stats)
            k = doc["kismet"]
            k["ts"] = {"sec": ts_sec, "usec": ts_usec}
            src_u = src.upper() if src else None
            dst_u = dst.upper() if dst else None
            derived: List[str] = []

            status, la, lo = classify(lat, lon)
            if _apply_geo(doc, status, la, lo, "observer", stats, raw=(lat, lon)):
                doc["gps"] = compact({"altitude": num(alt), "speed": num(speed), "heading": num(heading)})

            khz = num(freq)
            mhz = khz / 1000.0 if khz and khz > 0 else None
            is_wifi = phy == "IEEE802.11"
            wifi: Dict[str, Any] = {}
            k["phy"] = phy
            if is_wifi:
                band, ch = band_channel(mhz)
                wifi = {"phy": phy, "mac": src_u, "frequency": mhz, "band": band,
                        "channel": str(ch) if ch is not None else None,
                        "rssi": _zero_is_none(sig)}
                if band:
                    derived.append("wifi.band")
                if ch is not None:
                    derived.append("wifi.channel")
                if trans and trans != ZERO_MAC:
                    wifi["transmitter_mac"] = trans.upper()
            # Link to the device row (same file). packets.devkey is 0 in these captures, so the
            # link goes through the MAC address; every field taken this way is declared derived.
            sref = devices.get((phy, src_u)) if src_u else None
            dref = devices.get((phy, dst_u)) if dst_u else None
            if sref:
                k["device_key"] = sref.devkey
                k["device"] = compact({"type": sref.type})
                derived += ["kismet.device_key", "kismet.device.type"]
            if is_wifi:
                ap_ref, ap_mac = (sref, src_u) if (sref and sref.is_ap) else ((dref, dst_u) if (dref and dref.is_ap) else (None, None))
                if ap_ref:
                    wifi["bssid"] = ap_mac
                    derived.append("wifi.bssid")
                    if ap_ref.ssid:
                        wifi["ssid"] = ap_ref.ssid
                        derived.append("wifi.ssid")
                doc["wifi"] = compact(wifi)
            doc["source"] = compact({"mac": src_u})
            doc["destination"] = compact({"mac": dst_u})

            ds = _ds_fields(ctx, dsu)
            if ds:
                k["datasource"] = ds
                doc["observer"] = compact({"name": ds.get("name"), "type": ds.get("type")})
            k["packet"] = compact({
                "len": plen, "full_len": pfull, "hash": phash, "packet_id": pid, "dlt": dlt,
                "error": bool(err) if err is not None else None,
                "tags": [t for t in _TAG_SPLIT.split(tags) if t] if tags else None,
                "datarate": num(rate), "signal_raw": sig, "frequency_khz": khz,
                "devkey_raw": devkey_raw,
            })
            if derived:
                k["derived"] = derived
            stats.docs += 1
            yield "wifi.packet", ctx.doc_id("packets", rowid), doc
        except Exception as e:
            stats.reject(rowid, f"packet build failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- data (ADS-B frames etc.)
def data_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
              ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    for r in ctx.db.select("data", DATA_COLS, limit=limit):
        rowid, ts_sec, ts_usec, phy, devmac, lat, lon, alt, speed, heading, dsu, dtype, blob = r
        stats.rows += 1
        try:
            obj, err = blob_json(blob)
            adsb = phy == "ADSB" or dtype == "ADSB"
            record = "adsb.frame" if adsb else "data.event"
            doc = _base(ctx, record, "data", rowid, "kismet.adsb.frame" if adsb else "kismet.data", category=["network"])
            _apply_ts(doc, iso(ts_sec, ts_usec), stats)
            k = doc["kismet"]
            k["ts"] = {"sec": ts_sec, "usec": ts_usec}
            mac = str(devmac).upper() if devmac else None
            k.update(compact({"phy": phy, "devmac": mac}))
            if err:
                k["parse_error"] = err[:300]
                if err != "null":
                    stats.reject(rowid, f"data json: {err}")
            derived: List[str] = []
            status, la, lo = classify(lat, lon)
            if adsb:
                # For ADS-B frames the row's lat/lon/alt/speed/heading is the *aircraft's* decoded
                # position (tens to hundreds of km from the receiver, altitude in the thousands of
                # metres), not the receiver's.
                reason = None
                if status == OK and ctx.implausible_emitter(la, lo):
                    status, reason = INVALID, "implausible_range_from_collector"
                _apply_geo(doc, status, None if status != OK else la, None if status != OK else lo,
                           "emitter_reported", stats, reason, raw=(lat, lon))
                ref = ctx.devices.get((phy, mac)) if mac else None
                aircraft = {}
                if status == OK:
                    alt_v, alt_rejected = _altitude(alt, stats)
                    aircraft.update({"altitude": alt_v, "speed": num(speed), "heading": num(heading)})
                    if alt_rejected is not None:
                        aircraft["rejected"] = {"altitude": alt_rejected}
                if ref:
                    k["device_key"] = ref.devkey
                    aircraft.update({"icao": ref.icao, "callsign": ref.callsign, "registration": ref.registration})
                    derived += ["kismet.device_key", "aircraft.icao"]
                doc["aircraft"] = compact(aircraft)
                if isinstance(obj, dict):
                    doc["adsb"] = compact({"frame": obj.get("adsb")})
            else:
                # Unknown data type: keep it, and do not claim what the position means.
                _apply_geo(doc, status, la, lo, "unspecified", stats, raw=(lat, lon))
                k["data"] = {"type": dtype}
                if obj is not None:
                    k["raw"] = obj
            ds = _ds_fields(ctx, dsu)
            if ds:
                k["datasource"] = ds
                doc["observer"] = compact({"name": ds.get("name"), "type": ds.get("type")})
            if derived:
                k["derived"] = derived
            stats.docs += 1
            yield record, ctx.doc_id("data", rowid), doc
        except Exception as e:
            stats.reject(rowid, f"data build failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- alerts / messages / snapshots
def alert_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
               ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    for r in ctx.db.select("alerts", ALERT_COLS, limit=limit):
        rowid, ts_sec, ts_usec, phy, devmac, lat, lon, header, blob = r
        stats.rows += 1
        try:
            obj, err = blob_json(blob)
            if err and err != "null":
                stats.reject(rowid, f"alert json: {err}")
            obj = obj if isinstance(obj, dict) else {}
            doc = _base(ctx, "alert", "alerts", rowid, "kismet.alert", kind="alert", category=["intrusion_detection"])
            _apply_ts(doc, iso(ts_sec, ts_usec), stats)
            k = doc["kismet"]
            k["ts"] = {"sec": ts_sec, "usec": ts_usec}
            k["phy"] = phy
            a = "kismet.alert."
            sev = obj.get(a + "severity")
            k["alert"] = compact({
                "header": header or obj.get(a + "header"), "class": obj.get(a + "class"), "severity": sev,
                "hash": obj.get(a + "hash"), "phy_id": obj.get(a + "phy_id"), "channel": obj.get(a + "channel") or None,
                "device_key": obj.get(a + "device_key"),
            })
            if isinstance(sev, int):
                doc["event"]["severity"] = sev
            if obj.get(a + "text"):
                doc["message"] = obj[a + "text"]
            sm, dm, tm = obj.get(a + "source_mac"), obj.get(a + "dest_mac"), obj.get(a + "transmitter_mac")
            doc["source"] = compact({"mac": sm if sm and sm != ZERO_MAC else None})
            doc["destination"] = compact({"mac": dm if dm and dm != ZERO_MAC else None})
            if tm and tm != ZERO_MAC:
                doc["wifi"] = {"transmitter_mac": tm}
            status, la, lo = classify(lat, lon)
            _apply_geo(doc, status, la, lo, "observer", stats, raw=(lat, lon))
            if obj:
                k["raw"] = obj
            stats.docs += 1
            yield "alert", ctx.doc_id("alerts", rowid), doc
        except Exception as e:
            stats.reject(rowid, f"alert build failed: {type(e).__name__}: {e}")


def message_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
                 ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    for r in ctx.db.select("messages", MESSAGE_COLS, limit=limit):
        rowid, ts_sec, lat, lon, msgtype, message = r
        stats.rows += 1
        try:
            doc = _base(ctx, "message", "messages", rowid, "kismet.message", category=["process"])
            _apply_ts(doc, iso(ts_sec), stats)
            doc["kismet"]["ts"] = {"sec": ts_sec}
            if message:
                doc["message"] = message
            if msgtype:
                doc["log"] = {"level": msgtype}
            status, la, lo = classify(lat, lon)
            _apply_geo(doc, status, la, lo, "observer", stats, raw=(lat, lon))
            stats.docs += 1
            yield "message", ctx.doc_id("messages", rowid), doc
        except Exception as e:
            stats.reject(rowid, f"message build failed: {type(e).__name__}: {e}")


def snapshot_docs(ctx: FileContext, stats: TableStats, limit: Optional[int] = None
                  ) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    for r in ctx.db.select("snapshots", SNAPSHOT_COLS, limit=limit):
        rowid, ts_sec, ts_usec, lat, lon, snaptype, blob = r
        stats.rows += 1
        try:
            obj, err = blob_json(blob)
            if err and err not in ("null", "empty"):
                stats.reject(rowid, f"snapshot json: {err}")
            obj = obj if isinstance(obj, dict) else {}
            st = (snaptype or "").upper()
            record = {"GPS": "gps.snapshot", "RADIATION": "snapshot.radiation", "SYSTEM": "snapshot.system"}.get(
                st, f"snapshot.{st.lower() or 'unknown'}")
            doc = _base(ctx, record, "snapshots", rowid, f"kismet.snapshot.{st.lower() or 'unknown'}",
                        category=["host"] if st == "SYSTEM" else ["network"])
            _apply_ts(doc, iso(ts_sec, ts_usec), stats)
            k = doc["kismet"]
            k["ts"] = {"sec": ts_sec, "usec": ts_usec}
            k["snapshot"] = {"type": snaptype}
            status, la, lo = classify(lat, lon)
            _apply_geo(doc, status, la, lo, "observer", stats, raw=(lat, lon))
            if st == "GPS" and obj:
                gl = obj.get("kismet.gps.location") if isinstance(obj.get("kismet.gps.location"), dict) else {}
                doc["gps"] = compact({
                    "name": obj.get("kismet.gps.name"),
                    "fix": gl.get("kismet.common.location.fix"),
                    "altitude": num(gl.get("kismet.common.location.alt")),
                    "speed": num(gl.get("kismet.common.location.speed")),
                    "heading": num(gl.get("kismet.common.location.heading")),
                })
            if obj:  # RADIATION rows are `{}` in every observed capture: nothing to store
                k["raw"] = obj
            stats.docs += 1
            yield record, ctx.doc_id("snapshots", rowid), doc
        except Exception as e:
            stats.reject(rowid, f"snapshot build failed: {type(e).__name__}: {e}")
