"""KismetDB -> WiGLE CSV (WigleWifi-1.4).

One row per access point (and per SSID it advertised) per capture file, placed where the collector
heard that AP *strongest*: the position, time, RSSI and altitude of the packet with the highest signal
that carries a real GPS fix. Nothing is invented: an AP that was never heard with both a fix and a
signal has no row (it is counted in the stats, not silently dropped).

The .kismet files are opened read-only/immutable by kismetdb.open_readonly and never modified.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import __version__
from .geo import OK, classify, num
from .kismetdb import KismetDb
from .normalize import band_channel

COLUMNS = ["MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI", "CurrentLatitude", "CurrentLongitude",
           "AltitudeMeters", "AccuracyMeters", "Type"]
AP_TYPES = ("Wi-Fi AP",)
_CIPHER = {"TKIP": "TKIP", "AES-CCMP": "CCMP", "AES-CCMP256": "CCMP256", "AES-GCMP": "GCMP", "AES-GCMP256": "GCMP256",
           "WEP40": "WEP40", "WEP104": "WEP104"}
_VERSION = {"WPA1": "WPA", "WPA": "WPA", "WPA2": "WPA2", "WPA3": "WPA3"}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def wigle_auth(crypt: Optional[str]) -> Tuple[str, bool]:
    """Kismet crypt string ("WPA2 WPA2-PSK AES-CCMP") -> WiGLE capability string ("[WPA2-PSK-CCMP][ESS]").
    Returns (string, recognised)."""
    toks = (crypt or "").split()
    if not toks or toks == ["Open"] or toks == ["None"]:
        return "[ESS]", True
    ciphers: List[str] = []
    for t in toks:
        c = _CIPHER.get(t)
        if c and c not in ciphers:
            ciphers.append(c)
    parts: List[str] = []
    known = False
    for kis, wig in _VERSION.items():
        seen: List[str] = []
        for t in toks:
            if t.startswith(kis + "-"):
                km = t[len(kis) + 1:]
                if km not in seen:
                    seen.append(km)
        if not seen and kis in toks:  # e.g. "WPA2 AES-CCMP": version and cipher but no key-management token
            known = True
            parts.append("[" + "-".join([wig] + (["+".join(ciphers)] if ciphers else [])) + "]")
        for km in seen:
            known = True
            parts.append("[" + "-".join([f"{wig}-{km}"] + (["+".join(ciphers)] if ciphers else [])) + "]")
    if any(t.startswith("WEP") for t in toks):
        known = True
        parts.append("[WEP]")
    if not known:
        return "[ESS]", False
    return "".join(parts) + "[ESS]", True


def _ts(sec: Any) -> str:
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _channel(raw: Any, freq_khz: Any) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        pass
    f = num(freq_khz)
    if f:
        return band_channel(f / 1000.0 if f > 100000 else f)[1]
    return None


def new_stats() -> Dict[str, int]:
    return {"files": 0, "ap_devices": 0, "rows": 0, "ap_no_position": 0, "ap_no_signal": 0,
            "unrecognised_auth": 0, "ssid_control_chars_replaced": 0, "no_channel": 0}


def _aps(db: KismetDb, stats: Dict[str, int]) -> Dict[str, dict]:
    aps: Dict[str, dict] = {}
    marks = ",".join("?" * len(AP_TYPES))
    for devkey, mac, blob in db.con.execute(
            f"select devkey, devmac, device from devices where type in ({marks}) and phyname = 'IEEE802.11'", AP_TYPES):
        try:
            d = json.loads(blob if isinstance(blob, str) else bytes(blob).decode("utf-8", "replace"))
        except (TypeError, ValueError):
            continue
        d11 = d.get("dot11.device") if isinstance(d.get("dot11.device"), dict) else {}
        adv = d11.get("dot11.device.advertised_ssid_map")
        adv = list(adv.values()) if isinstance(adv, dict) else (adv if isinstance(adv, list) else [])
        ssids = []
        for a in adv:
            if isinstance(a, dict):
                ssids.append((a.get("dot11.advertisedssid.ssid") or "", a.get("dot11.advertisedssid.crypt_string"),
                              a.get("dot11.advertisedssid.channel")))
        if not ssids:
            ssids = [("", d.get("kismet.device.base.crypt"), None)]
        aps[(mac or "").upper()] = {"mac": (mac or d.get("kismet.device.base.macaddr") or "").upper(), "ssids": ssids,
                       "channel": d.get("kismet.device.base.channel"), "freq": d.get("kismet.device.base.frequency")}
        stats["ap_devices"] += 1
    return aps


def rows_for_file(db: KismetDb, stats: Dict[str, int]) -> Iterator[List[Any]]:
    stats["files"] += 1
    aps = _aps(db, stats)
    best: Dict[str, tuple] = {}  # AP MAC -> (signal, -ts, lat, lon, alt, freq)
    fixed = set()
    # packets.devkey is not populated by this Kismet version ('0'), so a packet is attributed to an AP by its
    # *transmitting* address (sourcemac = the BSSID for beacons, probe responses and AP-originated data).
    for devkey, ts, lat, lon, alt, sig, freq in db.con.execute(
            "select upper(sourcemac), ts_sec, lat, lon, alt, signal, frequency from packets "
            "where lat is not null and lon is not null and not (lat = 0 and lon = 0) and (error is null or error = 0)"):
        if devkey not in aps:
            continue
        st, la, lo = classify(lat, lon)
        if st != OK:
            continue
        fixed.add(devkey)
        if not sig:  # 0 = Kismet's "no signal"
            continue
        cand = (sig, -ts, la, lo, alt, freq)
        if devkey not in best or cand[:2] > best[devkey][:2]:
            best[devkey] = cand
    for devkey, ap in aps.items():
        if devkey not in best:
            stats["ap_no_signal" if devkey in fixed else "ap_no_position"] += 1
            continue
        sig, nts, la, lo, alt, freq = best[devkey]
        for ssid, crypt, ch in ap["ssids"]:
            auth, ok = wigle_auth(crypt)
            if not ok:
                stats["unrecognised_auth"] += 1
            clean = _CTRL.sub(" ", ssid)
            if clean != ssid:
                stats["ssid_control_chars_replaced"] += 1
            chan = _channel(ch, None) or _channel(ap["channel"], ap["freq"]) or _channel(None, freq)
            if chan is None:
                stats["no_channel"] += 1
            a = num(alt)
            stats["rows"] += 1
            yield [ap["mac"], clean, auth, _ts(-nts), "" if chan is None else chan, int(sig), f"{la:.7f}", f"{lo:.7f}",
                   f"{a:.1f}" if a is not None else "0", 0, "WIFI"]


def header_line(kismet_versions: List[str]) -> str:
    rel = ",".join(sorted(set(v for v in kismet_versions if v))) or "unknown"
    return (f"WigleWifi-1.4,appRelease=Kismet_Cartographer {__version__},model=Kismet,release={rel},"
            f"device=kismet,display=kismet,board=kismet,brand=kismet")


def write_csv(fh, files: List[str], stats: Dict[str, int], progress=None) -> None:
    versions: List[str] = []
    body = []
    w = None
    for i, path in enumerate(files, 1):
        db = KismetDb(path)
        try:
            versions.append(str(db.kismet_version or ""))
            if progress:
                progress(i, len(files), db.name)
            body.extend(rows_for_file(db, stats))
        finally:
            db.close()
    fh.write(header_line(versions) + "\n")
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(COLUMNS)
    body.sort(key=lambda r: (r[3], r[0], r[1]))
    w.writerows(body)
