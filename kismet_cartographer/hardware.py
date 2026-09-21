"""Brand / model / version facts about a device, taken from what Kismet stored for it.

Kismet's device JSON holds these in several places and in messy spellings, so the dashboard cannot
chart them directly. Everything here is a pure function of that JSON (no I/O), and every value it
returns is *derived*: the untouched originals stay in `kismet.device.manufacturer` and `kismet.raw`.

  brand     `clean_brand()`      "Vantiva USA LLC" / "Vantiva - Connected Home" -> "Vantiva"
  model     `wps_identity()`     the model name/number a Wi-Fi AP announces in its WPS information element
            `aircraft_identity()` maker + type parsed from the ICAO registry record Kismet attached
  version   `wifi_security()`    the WPA generation (WPA / WPA2 / WPA3 / mixed) from Kismet's crypt string
            `wps_identity()`     the WPS protocol version (1.0 / 2.0)
  width     `channel_width()`    20/40/80/160 MHz from the advertised HT/VHT mode

The WPS serial number is deliberately never read: it identifies one physical unit.
"""
import re
from typing import Any, Dict, Iterable, Optional, Tuple

_UNKNOWN = {"", "unknown", "adsb", "n/a", "na", "none", "null", "-", "default", "not specified", "undefined"}
_JUNK_MODEL = _UNKNOWN | {"model", "0", "1", "123", "1234", "12345", "123456", "1.0", "test", "wps"}

# Corporate-form words. The name is cut at the first of them, so "Foo Bar Co.,Ltd." -> "Foo Bar".
_SUFFIX = re.compile(
    r"[\s,，]+(?:co[\s.,，]*ltd\.?|co[\s.,，]*limited|ltd\.?|limited|inc\.?|incorporated|llc|l\.l\.c\.|lp|"
    r"corp(?:oration|orate)?\.?|company|gmbh|pte\.?|sas|s\.?\s?a\.?|ag|plc|headquarters|"
    r"international|a\s+\w+\s+company)(?=[\s,，.\-]|$).*$",
    re.IGNORECASE)

# Lower-case prefix -> display brand. Longest prefix wins; a prefix matches whole words only.
_ALIASES = [
    ("vantiva", "Vantiva"), ("technicolor", "Vantiva"),  # Vantiva = Technicolor Connected Home, renamed
    ("tp-link", "TP-Link"), ("tplink", "TP-Link"),
    ("hewlett packard enterprise", "HPE / Aruba"), ("hewlett packard", "HP"), ("hp", "HP"),
    ("cisco meraki", "Cisco Meraki"), ("cisco", "Cisco"), ("amazon", "Amazon"), ("samsung", "Samsung"),
    ("apple", "Apple"), ("asustek", "ASUS"), ("asus", "ASUS"), ("ampak", "AMPAK"), ("verizon", "Verizon"),
    ("google", "Google"), ("comcast", "Comcast"), ("commscope", "CommScope"), ("arris", "CommScope"),
    ("netgear", "Netgear"), ("ubiquiti", "Ubiquiti"), ("eero", "eero"), ("ruckus", "Ruckus"),
    ("airbus", "Airbus"), ("boeing", "Boeing"), ("bombardier", "Bombardier"), ("embraer", "Embraer"),
    ("mcdonnell douglas", "McDonnell Douglas"), ("cessna", "Cessna"), ("textron", "Textron"),
    ("gulfstream", "Gulfstream"), ("dassault", "Dassault"), ("piper", "Piper"), ("beech", "Beechcraft"),
    ("sikorsky", "Sikorsky"), ("bell", "Bell"), ("robinson", "Robinson"), ("cirrus", "Cirrus"),
]
_ALIASES.sort(key=lambda a: -len(a[0]))


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip() if s is not None else ""


def clean_brand(name: Any) -> Optional[str]:
    """A short, stable brand name, or None when the source has no usable one."""
    s = _norm(name)
    if s.lower() in _UNKNOWN:
        return None
    s = _SUFFIX.sub("", s).strip(" ,，.-")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)  # "Raspberry Pi (Trading)" -> "Raspberry Pi"
    if s.lower() in _UNKNOWN:
        return None
    key = s.lower()
    for prefix, brand in _ALIASES:
        if key == prefix or re.match(re.escape(prefix) + r"(?=[\s\-,.]|$)", key):
            return brand
    if s.isupper() and len(s) > 4:  # SHOUTING names -> Title Case, but short words (TCL, LG, HP) stay upper
        s = " ".join(w if len(w) <= 3 else w.title() for w in s.split(" "))
    return s or None


def clean_model(name: Any) -> Optional[str]:
    s = _norm(name)
    return None if s.lower() in _JUNK_MODEL or len(s) > 64 else s


def _ssid_records(d11: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Every advertised-SSID record Kismet kept for an AP, most recent beacon first."""
    last = d11.get("dot11.device.last_beaconed_ssid_record")
    if isinstance(last, dict):
        yield last
    for key in ("dot11.device.advertised_ssid_map", "dot11.device.responded_ssid_map"):
        v = d11.get(key)
        recs = list(v.values()) if isinstance(v, dict) else (v if isinstance(v, list) else [])
        for r in recs:
            if isinstance(r, dict):
                yield r


def wps_identity(d11: Dict[str, Any]) -> Dict[str, Any]:
    """What the AP itself announced in WPS: manufacturer, model name, model number, device name, version."""
    out: Dict[str, Any] = {}
    fields = {"manufacturer": "dot11.advertisedssid.wps_manuf", "model_name": "dot11.advertisedssid.wps_model_name",
              "model_number": "dot11.advertisedssid.wps_model_number",
              "device_name": "dot11.advertisedssid.wps_device_name", "version": "dot11.advertisedssid.wps_version"}
    for rec in _ssid_records(d11):
        for k, src in fields.items():
            if k not in out and rec.get(src) not in (None, "", 0):
                out[k] = rec[src]
    v = out.get("version")
    if v is not None:
        try:  # Kismet stores the raw byte: 16 = 0x10 = WPS 1.0, 32 = 0x20 = WPS 2.0
            n = int(v)
            out["version"] = f"{n >> 4}.{n & 0xF}"
        except (TypeError, ValueError):
            out.pop("version")
    for k in ("manufacturer", "model_name", "model_number", "device_name"):
        if k in out:
            out[k] = _norm(out[k]) or None
    return {k: v for k, v in out.items() if v}


def aircraft_identity(model: Any, icao_type: Any) -> Tuple[Optional[str], Optional[str]]:
    """(brand, model) from the ICAO record: model is "BOEING 737-8", type is "737-8"."""
    m, t = _norm(model), _norm(icao_type)
    if m.lower() in _UNKNOWN:
        return None, None
    if t and t.lower() not in _UNKNOWN and m.upper().endswith(t.upper()) and len(m) > len(t):
        return clean_brand(m[: len(m) - len(t)]), t
    return clean_brand(m.split(" ", 1)[0]), (m.split(" ", 1)[1] if " " in m else m)


def wifi_security(crypt: Any) -> Tuple[Optional[str], Optional[str]]:
    """(generation, auth) from Kismet's crypt string, e.g. "WPA3 WPA3-SAE AES-CCMP" -> ("WPA3", "Personal").

    generation: Open / Enhanced Open (OWE) / WEP / WPA / WPA2 / WPA2/WPA3 / WPA3.
    auth: Open / Personal (a shared password) / Enterprise (per-user 802.1X).
    """
    s = _norm(crypt).upper()
    if not s or s in ("UNKNOWN", "NONE"):
        return None, None
    toks = set(re.split(r"[\s,]+", s))
    joined = " ".join(toks)
    if "OWE" in joined:
        return "Enhanced Open (OWE)", "Open"
    if "WPA3" in toks or any(t.startswith("WPA3-") for t in toks):
        gen = "WPA2/WPA3" if ("WPA2" in toks or any(t.startswith("WPA2-") for t in toks)) else "WPA3"
    elif "WPA2" in toks or any(t.startswith("WPA2-") for t in toks):
        gen = "WPA2"
    elif "WPA" in toks or any(t.startswith("WPA-") for t in toks):
        gen = "WPA"
    elif "WEP" in toks:
        gen = "WEP"
    elif "OPEN" in toks:
        return "Open", "Open"
    else:
        return None, None
    if "EAP" in joined:
        return gen, "Enterprise"
    return gen, "Personal"


def channel_width(ht_mode: Any) -> Optional[str]:
    """"HT20" / "HT40+" / "HT80" / "HT80+80" -> "20 MHz" / "40 MHz" / "80 MHz" / "160 MHz"."""
    m = re.search(r"(\d+)(?:\+(\d+))?", _norm(ht_mode))
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2) and n == int(m.group(2)):  # 80+80 is two 80 MHz segments
        n *= 2
    return f"{n} MHz" if n in (20, 40, 80, 160, 320) else None


def ht_mode_of(d11: Dict[str, Any]) -> Optional[str]:
    for rec in _ssid_records(d11):
        if rec.get("dot11.advertisedssid.ht_mode"):
            return rec["dot11.advertisedssid.ht_mode"]
    return None
