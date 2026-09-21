"""A small, fully invented KismetDB capture.

Used by the unit tests and by `scripts/make_sample_capture.py` (to try the pipeline before you have captures of your own).
Nothing here comes from a real capture: brands, networks, aircraft and the route are made up, and the route is in an
arbitrary open area. The file has Kismet's real table layout and covers every case the pipeline has to handle:

  Wi-Fi APs (WPA2, WPA3, transition, enterprise, open, hidden SSID, WPS model/version, several channel widths),
  clients, bridged devices, a device with Kismet's garbage centroid, a device with malformed JSON,
  packets with GPS, without a fix (0,0), with signal 0, and with an impossible latitude,
  aircraft with and without a position, ADS-B frames with decode errors and an absurd altitude,
  an unknown `data` type, alerts, messages, GPS / RADIATION / SYSTEM / unknown snapshots, two datasources.

Deterministic: the same seed always produces the same rows.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_TS = 1735689600            # 2025-01-01T00:00:00Z
START = (40.0000, -100.0000)    # an arbitrary point; the route runs east from here
STEP_LON = 0.004
ROUTE_POINTS = 60
KISMET_VERSION = "2025.09.0"

WIFI_DS = "11111111-2222-3333-4444-000000000001"
ADSB_DS = "11111111-2222-3333-4444-000000000002"

SCHEMA = [
    "CREATE TABLE KISMET (kismet_version TEXT, db_version INT, db_module TEXT)",
    "CREATE TABLE devices (first_time INT, last_time INT, devkey TEXT, phyname TEXT, devmac TEXT, strongest_signal INT, "
    "min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL, avg_lat REAL, avg_lon REAL, bytes_data INT, type TEXT, "
    "device BLOB, UNIQUE(phyname, devmac) ON CONFLICT REPLACE)",
    "CREATE TABLE packets (ts_sec INT, ts_usec INT, phyname TEXT, sourcemac TEXT, destmac TEXT, transmac TEXT, "
    "frequency REAL, devkey TEXT, lat REAL, lon REAL, alt REAL, speed REAL, heading REAL, packet_len INT, signal INT, "
    "datasource TEXT, dlt INT, packet BLOB, error INT, tags TEXT, datarate REAL, hash INT, packetid INT, "
    "packet_full_len INT)",
    "CREATE TABLE data (ts_sec INT, ts_usec INT, phyname TEXT, devmac TEXT, lat REAL, lon REAL, alt REAL, speed REAL, "
    "heading REAL, datasource TEXT, type TEXT, json BLOB )",
    "CREATE TABLE datasources (uuid TEXT, typestring TEXT, definition TEXT, name TEXT, interface TEXT, json BLOB, "
    "UNIQUE(uuid) ON CONFLICT REPLACE)",
    "CREATE TABLE alerts (ts_sec INT, ts_usec INT, phyname TEXT, devmac TEXT, lat REAL, lon REAL, header TEXT, json BLOB )",
    "CREATE TABLE messages (ts_sec INT, lat REAL, lon REAL, msgtype TEXT, message TEXT )",
    "CREATE TABLE snapshots (ts_sec INT, ts_usec INT, lat REAL, lon REAL, snaptype TEXT, json BLOB )",
]


def route(i: int) -> Tuple[float, float]:
    """The i-th point of the invented collector route (slightly wavy, running east)."""
    return round(START[0] + 0.0035 * i + 0.002 * ((i % 5) - 2), 6), round(START[1] + STEP_LON * i, 6)


def _j(obj: Any) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _seen(ts_first: int, ts_last: int, uuid: str) -> List[Dict[str, Any]]:
    return [{"kismet.common.seenby.uuid": uuid, "kismet.common.seenby.first_time": ts_first,
             "kismet.common.seenby.last_time": ts_last}]


def _wifi_json(key, mac, dtype, manuf, crypt, channel, khz, ssid, first, last, pt, rssi, *, hidden=False, ht="HT20",
               wps=None, probed=(), bounds_peak=True, avg_geo=None, clients=0):
    lat, lon = pt
    rec = {"dot11.advertisedssid.ssid": "" if hidden else ssid, "dot11.advertisedssid.ht_mode": ht,
           "dot11.advertisedssid.crypt_string": crypt}
    rec.update(wps or {})
    d11: Dict[str, Any] = {"dot11.device.num_associated_clients": clients, "dot11.device.last_bssid": mac}
    if dtype == "Wi-Fi AP":
        d11["dot11.device.advertised_ssid_map"] = [rec]
        d11["dot11.device.last_beaconed_ssid_record"] = dict(rec)
    if probed:
        d11["dot11.device.probed_ssid_map"] = [{"dot11.probedssid.ssid": s} for s in probed]
    signal: Dict[str, Any] = {"kismet.common.signal.last_signal": rssi}
    if bounds_peak:
        signal["kismet.common.signal.peak_loc"] = {"kismet.common.location.geopoint": [lon, lat]}
    return {"kismet.device.base.key": key, "kismet.device.base.macaddr": mac, "kismet.device.base.phyname": "IEEE802.11",
            "kismet.device.base.type": dtype, "kismet.device.base.name": ssid or mac,
            "kismet.device.base.commonname": ssid or mac, "kismet.device.base.manuf": manuf,
            "kismet.device.base.crypt": crypt, "kismet.device.base.channel": str(channel),
            "kismet.device.base.frequency": khz, "kismet.device.base.first_time": first,
            "kismet.device.base.last_time": last, "kismet.device.base.packets.total": 100,
            "kismet.device.base.signal": signal,
            "kismet.device.base.location": {"kismet.common.location.avg_loc": {
                "kismet.common.location.geopoint": avg_geo or [lon, lat]}},
            "kismet.device.base.seenby": _seen(first, last, WIFI_DS), "dot11.device": d11}


def _air_json(key, mac, icao, call, reg, model, typ, owner, atype, pt, first, last):
    lat, lon = pt
    return {"kismet.device.base.key": key, "kismet.device.base.macaddr": mac, "kismet.device.base.phyname": "ADSB",
            "kismet.device.base.type": "Airplane", "kismet.device.base.manuf": "ADSB",
            "kismet.device.base.first_time": first, "kismet.device.base.last_time": last,
            "kismet.device.base.location": {"kismet.common.location.last": {
                "kismet.common.location.geopoint": [lon, lat], "kismet.common.location.alt": 10500.0,
                "kismet.common.location.speed": 230.0, "kismet.common.location.heading": 90.0}},
            "kismet.device.base.seenby": _seen(first, last, ADSB_DS),
            "adsb.device": {"adsb.device.icao": icao, "adsb.device.callsign": call + " ",
                            "kismet.adsb.icao_record": {"adsb.icao.regid": reg, "adsb.icao.model": model,
                                                        "adsb.icao.type": typ, "adsb.icao.owner": owner,
                                                        "adsb.icao.atype": atype}}}


def _wps(manuf, name, number, ver, dev, serial="SN-DO-NOT-INDEX-0001"):
    return {"dot11.advertisedssid.wps_manuf": manuf, "dot11.advertisedssid.wps_model_name": name,
            "dot11.advertisedssid.wps_model_number": number, "dot11.advertisedssid.wps_version": ver,
            "dot11.advertisedssid.wps_device_name": dev, "dot11.advertisedssid.wps_serial_number": serial}


# (mac, ssid, manuf(OUI name), crypt, channel, khz, ht_mode, wps, hidden)
_APS = [
    ("02:AA:00:00:00:01", "CoffeeShop-Guest", "Acme Wireless Co.,Ltd.", "WPA2 WPA2-PSK AES-CCMP", 6, 2437000, "HT20",
     _wps("Acme Wireless", "AW-100", "1.2", 16, "Acme Gateway"), False),
    ("02:AA:00:00:00:02", "Zephyr5G", "Zephyr Networks, Inc.", "WPA3 WPA3-PSK WPA3-SAE AES-CCMP", 36, 5180000, "HT80", None, False),
    ("02:AA:00:00:00:03", "", "Unknown", "WPA2 WPA2-PSK AES-CCMP", 11, 2462000, "HT20", None, True),
    ("02:AA:00:00:00:04", "FreeWiFi", "Unknown", "Open", 1, 2412000, "HT20",
     _wps("Borealis Devices", "BD-7", "7", 32, "Borealis AP"), False),
    ("02:AA:00:00:00:05", "CorpNet", "Zephyr Networks, Inc.", "WPA2 WPA2-EAP AES-CCMP", 149, 5745000, "HT40+", None, False),
    ("02:AA:00:00:00:06", "Transition", "Acme Wireless Co.,Ltd.", "WPA2 WPA2-PSK WPA3 WPA3-SAE AES-CCMP", 44, 5220000, "HT160", None, False),
    ("02:AA:00:00:00:07", "OldRouter", "Borealis Devices LLC", "WPA WPA-PSK TKIP", 3, 2422000, "HT20", None, False),
    ("02:AA:00:00:00:08", "Zephyr2G", "ZEPHYR NETWORKS INC", "WPA2 WPA2-PSK AES-CCMP", 1, 2412000, "HT20",
     _wps("Zephyr", "ZX-2", "2.0", 16, "ZX router"), False),
    ("02:AA:00:00:00:09", "Acme-Home", "Acme Wireless Co.,Ltd.", "WPA2 WPA2-PSK AES-CCMP", 6, 2437000, "HT20",
     _wps("Acme Wireless", "AW-100", "1.3", 16, "Acme Gateway"), False),
    ("02:AA:00:00:00:0A", "Borealis-Office", "Borealis Devices LLC", "WPA3 WPA3-SAE AES-CCMP", 100, 5500000, "HT80", None, False),
]
_CLIENTS = [("02:CC:00:00:00:%02X" % i, m, probes) for i, (m, probes) in enumerate([
    ("Acme Wireless Co.,Ltd.", ("CoffeeShop-Guest",)), ("Zephyr Networks, Inc.", ("HomeNet", "CorpNet")),
    ("Borealis Devices LLC", ()), ("Unknown", ("FreeWiFi",)), ("Acme Wireless Co.,Ltd.", ()),
    ("Zephyr Networks, Inc.", ("Zephyr5G",))], start=1)]
_PLANES = [  # icao, callsign, reg, model, type, owner, category
    ("A00001", "EX101", "N101EX", "SKYWORKS SW-100", "SW-100", "EXAMPLE AIR LINES INC", "L2J"),
    ("A00002", "EX102", "N102EX", "SKYWORKS SW-100", "SW-100", "EXAMPLE AIR LINES INC", "L2J"),
    ("A00003", "FT201", "N201FT", "FICTION AEROSPACE FA-20", "FA-20", "FICTIONAL TRANSPORT LLC", "L2J"),
    ("A00004", "FT202", "N202FT", "FICTION AEROSPACE CANADA LP FA-500", "FA-500", "FICTIONAL TRANSPORT LLC", "L2J"),
    ("A00005", "", "N305PR", "PLAINS AIRCRAFT 172X", "172X", "PRIVATE OWNER", "L1P"),
    ("A00006", "EX103", "N103EX", "SKYWORKS SW-200 S A", "SW-200", "EXAMPLE AIR LINES INC", "L2J"),
    ("A00007", "", "N999ZZ", "Unknown", "Unknown", "Unknown", ""),
]


def build(path, seed: int = 0) -> Dict[str, int]:
    """Write the synthetic capture to `path` (replacing any existing file). Returns the row counts per table."""
    rnd = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    for ddl in SCHEMA:
        con.execute(ddl)
    con.execute("insert into KISMET values (?,?,?)", (KISMET_VERSION, 9, ""))
    last = BASE_TS + ROUTE_POINTS * 30

    # ---- datasources
    con.execute("insert into datasources values (?,?,?,?,?,?)",
                (WIFI_DS, "linuxwifi", "wlan1:name=wlan1", "wlan1", "wlan1", _j({"kismet.datasource.hardware": "Example Wi-Fi adapter"})))
    con.execute("insert into datasources values (?,?,?,?,?,?)",
                (ADSB_DS, "rtladsb", "rtladsb-0", "rtladsb-0", "rtladsb-0", _j({"kismet.datasource.hardware": "Example SDR"})))

    # ---- devices
    def add_device(key, phy, mac, dtype, blob, bounds, avg, strongest=-55):
        con.execute("insert into devices values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (BASE_TS, last, key, phy, mac, strongest, *bounds, *avg, 1000, dtype, blob))

    span = (route(0), route(ROUTE_POINTS - 1))
    box = (min(p[0] for p in span), min(p[1] for p in span), max(p[0] for p in span), max(p[1] for p in span))
    mid = route(ROUTE_POINTS // 2)
    macs = []
    for n, (mac, ssid, manuf, crypt, ch, khz, ht, wps, hidden) in enumerate(_APS):
        pt = route(5 + n * 4)
        add_device(f"K_AP_{n}", "IEEE802.11", mac, "Wi-Fi AP",
                   _j(_wifi_json(f"K_AP_{n}", mac, "Wi-Fi AP", manuf, crypt, ch, khz, ssid, BASE_TS, last, pt, -60 - n,
                                 hidden=hidden, ht=ht, wps=wps, clients=n % 4)),
                   box, mid)
        macs.append((mac, khz))
    for mac, manuf, probes in _CLIENTS:
        pt = route(10)
        add_device("K_CL_" + mac[-2:], "IEEE802.11", mac, "Wi-Fi Client",
                   _j(_wifi_json("K_CL_" + mac[-2:], mac, "Wi-Fi Client", manuf, "Unknown", 6, 2437000, "", BASE_TS, last,
                                 pt, -70, probed=probes)), box, mid)
        macs.append((mac, 2437000))
    for n, dtype in enumerate(("Wi-Fi Bridged", "Wi-Fi Bridged", "Wi-Fi Device")):
        mac = "02:BB:00:00:00:%02X" % (n + 1)
        add_device(f"K_BR_{n}", "IEEE802.11", mac, dtype,
                   _j(_wifi_json(f"K_BR_{n}", mac, dtype, "Borealis Devices LLC", "Unknown", 6, 2437000, "", BASE_TS, last,
                                 mid, -75)), box, mid)
    # Kismet sometimes writes a garbage average position that lies outside the points it averaged
    for n, (geo_pt, peak) in enumerate((([90.0, 0.0], True), ([90.0, 90.0], True), ([90.0, 0.0], False))):
        mac = "02:DD:00:00:00:%02X" % (n + 1)
        add_device(f"K_GARBAGE_{n}", "IEEE802.11", mac, "Wi-Fi Client",
                   _j(_wifi_json(f"K_GARBAGE_{n}", mac, "Wi-Fi Client", "Acme Wireless Co.,Ltd.", "Unknown", 6, 2437000, "",
                                 BASE_TS, last, mid, -80, bounds_peak=peak, avg_geo=geo_pt)),
                   (mid[0], mid[1], mid[0], mid[1]), (0.0, 90.0))
    # malformed JSON in the device blob
    con.execute("insert into devices values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (BASE_TS, BASE_TS + 1, "SYNTH_BAD_JSON", "IEEE802.11", "DE:AD:BE:EF:00:01", -70, 40.0, -100.0, 40.0, -100.0,
                 40.0, -100.0, 0, "Wi-Fi AP", b'{"kismet.device.base.key": "SYNTH", this is not valid json'))
    # aircraft: 5 with a position, 2 without (Kismet writes [0, 0])
    for n, (icao, call, reg, model, typ, owner, cat) in enumerate(_PLANES):
        mac = "0A:D5:B0:00:00:%02X" % (n + 1)
        has_pos = n < 5
        pt = (round(40.05 + 0.04 * n, 5), round(-99.95 + 0.05 * n, 5)) if has_pos else (0.0, 0.0)
        add_device(f"K_AIR_{n}", "ADSB", mac, "Airplane",
                   _j(_air_json(f"K_AIR_{n}", mac, icao, call, reg, model, typ, owner, cat, pt, BASE_TS + 60, last - 60)),
                   (pt[0], pt[1], pt[0], pt[1]), pt, strongest=0)
    planes = [("0A:D5:B0:00:00:%02X" % (n + 1), (round(40.05 + 0.04 * n, 5), round(-99.95 + 0.05 * n, 5))) for n in range(5)]

    # ---- packets: several observations of each device at different places, times and signals
    pk = ("insert into packets (ts_sec,ts_usec,phyname,sourcemac,destmac,transmac,frequency,devkey,lat,lon,alt,speed,"
          "heading,packet_len,signal,datasource,dlt,error,tags,datarate,hash,packetid,packet_full_len,packet) "
          "values (?,?,'IEEE802.11',?,?,'00:00:00:00:00:00',?,'0',?,?,?,?,?,?,?,?,127,0,'',6.0,?,?,?,x'00')")
    pid = 0

    def add_packet(ts, src, khz, lat, lon, signal):
        nonlocal pid
        pid += 1
        con.execute(pk, (ts, rnd.randrange(1_000_000), src, "FF:FF:FF:FF:FF:FF", khz, lat, lon, 350.0, 12.0, 90.0,
                         rnd.randrange(60, 400), signal, WIFI_DS, pid, pid, 400))

    for mac, khz in macs:
        for k in range(10):
            i = rnd.randrange(ROUTE_POINTS)
            lat, lon = route(i)
            add_packet(BASE_TS + i * 30 + k, mac, khz, lat, lon, rnd.randrange(-88, -35))
    for k in range(40):                                   # collector had no GPS fix: Kismet writes (0, 0)
        add_packet(BASE_TS + 2000 + k, macs[k % len(macs)][0], 2437000, 0.0, 0.0, rnd.randrange(-88, -35))
    for k in range(12):                                   # signal 0 is Kismet's "unknown", not a real power
        lat, lon = route(k + 3)
        add_packet(BASE_TS + 2100 + k, macs[k % len(macs)][0], 2437000, lat, lon, 0)
    add_packet(BASE_TS + 2200, "AA:BB:CC:00:00:03", 2437000, 95.5, -100.0, -60)   # impossible latitude

    # ---- data: ADS-B frames and odd rows
    dq = ("insert into data (ts_sec,ts_usec,phyname,devmac,lat,lon,alt,speed,heading,datasource,type,json) "
          "values (?,5,?,?,?,?,?,?,?,?,?,?)")
    frame = _j({"adsb": "*8d0000000000000000000000000000;"})
    for mac, (la, lo) in planes:
        for k in range(6):
            con.execute(dq, (BASE_TS + 100 + k * 20, "ADSB", mac, la + 0.01 * k, lo + 0.01 * k, 10500.0, 230.0, 90.0,
                             ADSB_DS, "ADSB", frame))
    for k in range(12):                                   # frames without a decoded position
        con.execute(dq, (BASE_TS + 500 + k, "ADSB", "0A:D5:B0:00:00:06", 0.0, 0.0, 0.0, 0.0, 0.0, ADSB_DS, "ADSB", frame))
    con.execute(dq, (BASE_TS + 600, "FOO", "AA:BB:CC:00:00:01", 40.05, -99.9, 0, 0, 0, "", "FOO", _j({"payload": 42})))
    con.execute(dq, (BASE_TS + 601, "ADSB", "AA:BB:CC:00:00:02", 0.0, 0.0, 0, 0, 0, "", "ADSB", b"not json at all"))
    # ADS-B decode errors: an impossible latitude, and a legal point thousands of km from the receiver
    for i, (la, lo) in enumerate(((112.2495, -146.1182), (2.5318, 2.3331)), start=1):
        con.execute(dq, (BASE_TS + 610 + i, "ADSB", f"AA:BB:CC:00:01:0{i}", la, lo, 11582.4, 0, 0, "", "ADSB", frame))
    # a plausible position but Kismet's absurd altitude artefact
    con.execute(dq, (BASE_TS + 620, "ADSB", "AA:BB:CC:00:01:09", 40.05, -99.85, 5.622567663155806e18, 700, 90, "", "ADSB", frame))

    # ---- alerts, messages, snapshots
    for k in range(4):
        lat, lon = route(k * 7)
        con.execute("insert into alerts values (?,?,?,?,?,?,?,?)",
                    (BASE_TS + 300 + k, 1, "IEEE802.11", "02:AA:00:00:00:0%d" % (k + 1), lat, lon, "PROBERESP",
                     _j({"kismet.alert.header": "PROBERESP", "kismet.alert.class": "PROBE", "kismet.alert.severity": 5,
                         "kismet.alert.text": "example alert %d" % k, "kismet.alert.source_mac": "02:AA:00:00:00:0%d" % (k + 1),
                         "kismet.alert.dest_mac": "FF:FF:FF:FF:FF:FF", "kismet.alert.transmitter_mac": "00:00:00:00:00:00"})))
    for k in range(6):
        lat, lon = route(k * 9)
        con.execute("insert into messages values (?,?,?,?,?)", (BASE_TS + k, lat, lon, "INFO", "example message %d" % k))
        con.execute("insert into messages values (?,?,?,?,?)", (BASE_TS + 10 + k, 0.0, 0.0, "INFO", "message without a fix %d" % k))
    for k in range(8):
        lat, lon = route(k * 6)
        con.execute("insert into snapshots values (?,?,?,?,?,?)",
                    (BASE_TS + k * 30, 0, lat, lon, "GPS",
                     _j({"kismet.gps.name": "gps1", "kismet.gps.location": {
                         "kismet.common.location.fix": 3, "kismet.common.location.alt": 350.2,
                         "kismet.common.location.speed": 12.5, "kismet.common.location.heading": 90.0}})))
    for k in range(4):
        lat, lon = route(k)
        con.execute("insert into snapshots values (?,?,?,?,?,?)", (BASE_TS + k, 0, lat, lon, "RADIATION", b"{}"))
    for k in range(3):
        con.execute("insert into snapshots values (?,?,?,?,?,?)",
                    (BASE_TS + k * 60, 0, 0.0, 0.0, "SYSTEM", _j({"kismet.system.timestamp": BASE_TS + k * 60})))
    con.execute("insert into snapshots values (?,?,?,?,?,?)", (BASE_TS + 700, 0, mid[0], mid[1], "WEIRD", _j({"a": 1})))
    con.commit()
    counts = {t: con.execute(f"select count(*) from {t}").fetchone()[0]
              for t in ("devices", "packets", "data", "alerts", "messages", "snapshots", "datasources")}
    con.close()
    return counts
