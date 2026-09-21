"""Offline tests for extraction + normalisation. No Elasticsearch and no captures of your own needed.

    python3 -m unittest discover -s tests -v

The pipeline tests run on a small invented capture built on the fly by kismet_cartographer.synthetic
(nothing in it comes from a real capture).
"""
import hashlib
import sqlite3
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kismet_cartographer import geo, hardware  # noqa: E402
from kismet_cartographer import config as kc_config  # noqa: E402
from kismet_cartographer import synthetic  # noqa: E402
from kismet_cartographer import cli_common  # noqa: E402
from kismet_cartographer.ingest import Options, iter_documents, new_stats, RejectLog  # noqa: E402
from kismet_cartographer.kismetdb import KismetDb, file_sha256  # noqa: E402
from kismet_cartographer.normalize import band_channel, iso  # noqa: E402

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GeoTests(unittest.TestCase):
    def test_zero_zero_is_no_fix_not_a_location(self):
        self.assertEqual(geo.classify(0.0, 0.0)[0], geo.NO_FIX)
        self.assertEqual(geo.classify(None, None)[0], geo.NO_FIX)

    def test_valid_and_invalid(self):
        self.assertEqual(geo.classify(40.66, -100.24), (geo.OK, 40.66, -100.24))
        self.assertEqual(geo.classify(95.5, -100.2)[0], geo.INVALID)
        self.assertEqual(geo.classify(38.0, -181.0)[0], geo.INVALID)
        self.assertEqual(geo.classify(float("nan"), 1.0)[0], geo.INVALID)
        self.assertEqual(geo.classify("x", 1.0)[0], geo.INVALID)
        self.assertEqual(geo.classify(0.0, 12.5)[0], geo.OK)  # only (0,0) together is a sentinel

    def test_kismet_geopoint_is_lon_lat(self):
        # Kismet JSON geopoint = [lon, lat]; the resulting point must have lat=40.66, lon=-100.24
        self.assertEqual(geo.from_kismet_geopoint([-100.24, 40.66]), (geo.OK, 40.66, -100.24))
        self.assertEqual(geo.from_kismet_geopoint([0, 0])[0], geo.NO_FIX)
        self.assertEqual(geo.from_kismet_geopoint(None)[0], geo.NO_FIX)


class ConnectionFlagTests(unittest.TestCase):
    """The installer's promise: URL + key + CA given as flags are all that is needed, and flags win."""

    def parse(self, argv):
        import argparse
        ap = argparse.ArgumentParser()
        kc_config.add_connection_args(ap, kibana=True)
        return ap.parse_args(argv)

    def setUp(self):
        # Isolate from the machine the tests run on: no environment settings, no per-user or project env files, and a
        # working directory without a .env, so a developer's real credentials can never leak into (or out of) a test.
        import os, tempfile
        self._saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("ELASTIC_", "KIBANA_"))}
        self._tmp = tempfile.TemporaryDirectory()
        self._patched = (kc_config.DEFAULT_USER_ENV, kc_config.REPO_ROOT, os.getcwd())
        kc_config.DEFAULT_USER_ENV = Path(self._tmp.name) / "none" / "credentials.env"
        kc_config.REPO_ROOT = Path(self._tmp.name) / "repo"
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._patched[2])
        kc_config.DEFAULT_USER_ENV, kc_config.REPO_ROOT = self._patched[0], self._patched[1]
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_flags_are_applied_and_one_key_serves_both(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".crt") as ca:
            a = self.parse(["--es-url", "https://es:9200/", "--kibana-url", "https://kb:5601", "--api-key", "id:secret",
                            "--ca", ca.name, "--space", "team"])
            cfg = kc_config.load_config(args=a)
            self.assertEqual(cfg.elastic_url, "https://es:9200")
            self.assertEqual(cfg.kibana_url, "https://kb:5601")
            self.assertEqual(cfg.api_key, cfg.install_api_key)
            self.assertEqual(cfg.api_key, cfg.kibana_api_key)
            self.assertEqual(cfg.ca_cert, ca.name)
            self.assertEqual(cfg.kibana_ca_cert, ca.name)  # --ca covers Kibana unless --kibana-ca is given
            self.assertEqual(cfg.space, "team")

    def test_api_key_forms_and_file(self):
        import base64, os, tempfile
        self.assertEqual(kc_config.normalize_api_key("abc:def"), base64.b64encode(b"abc:def").decode())
        self.assertEqual(kc_config.normalize_api_key("ApiKey QUJD"), "QUJD")
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as f:
            f.write("QUJDREVG==\n")
        try:
            os.chmod(f.name, 0o600)
            cfg = kc_config.load_config(args=self.parse(["--api-key-file", f.name]))
            self.assertEqual(cfg.api_key, "QUJDREVG==")
        finally:
            os.unlink(f.name)

    def test_flags_beat_environment_and_missing_ca_file_is_an_error(self):
        import os
        os.environ["ELASTIC_URL"], os.environ["ELASTIC_API_KEY"] = "https://from-env:9200", "ZW52"
        self.assertEqual(kc_config.load_config(args=self.parse(["--es-url", "https://flag:9200"])).elastic_url,
                         "https://flag:9200")
        self.assertEqual(kc_config.load_config(args=self.parse([])).elastic_url, "https://from-env:9200")
        with self.assertRaises(SystemExit):
            kc_config.load_config(args=self.parse(["--ca", "/no/such/ca.crt"]))

    def test_host_shortcut_builds_urls_and_picks_scheme(self):
        import tempfile
        h = lambda argv: (lambda c: (c.elastic_url, c.kibana_url))(kc_config.load_config(args=self.parse(argv)))  # noqa: E731
        self.assertEqual(h(["--host", "10.0.0.5"]), ("http://10.0.0.5:9200", "http://10.0.0.5:5601"))
        self.assertEqual(h(["--host", "10.0.0.5", "--https"]), ("https://10.0.0.5:9200", "https://10.0.0.5:5601"))
        self.assertEqual(h(["--host", "10.0.0.5", "--es-port", "19200", "--kibana-port", "15601"]),
                         ("http://10.0.0.5:19200", "http://10.0.0.5:15601"))
        self.assertEqual(h(["--host", "::1"])[0], "http://[::1]:9200")
        with tempfile.NamedTemporaryFile("w", suffix=".crt") as ca:  # giving a CA implies https
            self.assertEqual(h(["--host", "es.lan", "--ca", ca.name])[0], "https://es.lan:9200")
        self.assertEqual(h(["--host", "10.0.0.5", "--es-url", "https://es.example.com:9200"]),
                         ("https://es.example.com:9200", "http://10.0.0.5:5601"))  # an explicit URL wins

    def test_saved_settings_hold_no_secret_and_are_read_back(self):
        import os, stat, tempfile
        with tempfile.TemporaryDirectory() as d:
            key, ca, env = os.path.join(d, "kc.key"), os.path.join(d, "ca.crt"), os.path.join(d, "saved.env")
            open(key, "w").write("c2VjcmV0LWtleQ==\n")
            os.chmod(key, 0o600)
            open(ca, "w").write("pem")
            a = self.parse(["--es-url", "https://es:9200", "--kibana-url", "https://kb:5601", "--api-key-file", key,
                            "--ca", ca, "--space", "team"])
            cfg = kc_config.load_config(args=a)
            written = cli_common.write_env_file(cfg, env, key)
            text = open(env).read()
            self.assertNotIn("c2VjcmV0LWtleQ==", text)                      # the key itself is never written
            self.assertIn("ELASTIC_API_KEY_FILE=" + key, text)              # only where to find it
            self.assertIn("ELASTIC_CA_CERT=" + ca, text)
            self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o600)
            self.assertIn("ELASTIC_URL", written)
            back = kc_config.load_config(args=self.parse(["--env-file", env]))   # a later step needs no flags
            self.assertEqual((back.elastic_url, back.kibana_url, back.space), ("https://es:9200", "https://kb:5601", "team"))
            self.assertEqual(back.api_key, "c2VjcmV0LWtleQ==")
            other = os.path.join(d, "mine.env")                              # never clobber a file we did not write
            open(other, "w").write("MY_OWN=setting\n")
            with self.assertRaises(FileExistsError):
                cli_common.write_env_file(cfg, other, key)
            self.assertEqual(open(other).read(), "MY_OWN=setting\n")

    def test_capture_files_lists_only_kismet_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cli_common.capture_files(d), [])                # empty folder: nothing to load, not an error
            (Path(d) / "b.kismet").write_text("x")
            (Path(d) / "a.kismet").write_text("x")
            (Path(d) / "README.md").write_text("x")
            self.assertEqual([p.name for p in cli_common.capture_files(d)], ["a.kismet", "b.kismet"])
            self.assertEqual([p.name for p in cli_common.capture_files(Path(d) / "a.kismet")], ["a.kismet"])
            self.assertEqual(cli_common.capture_files(Path(d) / "missing"), [])

    def test_kibana_space_path(self):
        from kismet_cartographer.esclient import kibana_space_path
        self.assertEqual([kibana_space_path(x) for x in ("", "default", "team-a")], ["", "", "/s/team-a"])


class HardwareTests(unittest.TestCase):
    def test_brand_names_are_merged_and_unknowns_dropped(self):
        b = hardware.clean_brand
        self.assertEqual(b("Vantiva USA LLC"), "Vantiva")
        self.assertEqual(b("Vantiva - Connected Home"), "Vantiva")
        self.assertEqual(b("Technicolor"), "Vantiva")  # renamed; one brand, not two
        self.assertEqual(b("TP-LINK TECHNOLOGIES CO.,LTD."), "TP-Link")
        self.assertEqual(b("TP-Link Systems Inc"), "TP-Link")
        self.assertEqual(b("Hewlett Packard Enterprise "), "HPE / Aruba")
        self.assertEqual(b("HP Inc."), "HP")
        self.assertEqual(b("ALPSALPINE CO,.LTD"), "Alpsalpine")
        self.assertEqual(b("TCL MOKA International Limited"), "TCL Moka")
        self.assertEqual(b("Raspberry Pi (Trading) Ltd"), "Raspberry Pi")
        for unknown in ("Unknown", "ADSB", "", None, "  "):
            self.assertIsNone(b(unknown))

    def test_aircraft_maker_and_type_are_split_from_the_icao_model(self):
        self.assertEqual(hardware.aircraft_identity("BOEING 737-8", "737-8"), ("Boeing", "737-8"))
        self.assertEqual(hardware.aircraft_identity("AIRBUS CANADA LP BD-500-1A11", "BD-500-1A11"), ("Airbus", "BD-500-1A11"))
        self.assertEqual(hardware.aircraft_identity("EMBRAER S A ERJ 170-200 LR", "ERJ 170-200 LR"), ("Embraer", "ERJ 170-200 LR"))
        self.assertEqual(hardware.aircraft_identity("Unknown", "Unknown"), (None, None))

    def test_security_generation_and_auth(self):
        w = hardware.wifi_security
        self.assertEqual(w("WPA2 WPA2-PSK AES-CCMP"), ("WPA2", "Personal"))
        self.assertEqual(w("WPA3 WPA3-PSK WPA3-SAE AES-CCMP"), ("WPA3", "Personal"))
        self.assertEqual(w("WPA2 WPA2-PSK WPA3 WPA3-SAE AES-CCMP"), ("WPA2/WPA3", "Personal"))
        self.assertEqual(w("WPA2 WPA2-EAP AES-CCMP"), ("WPA2", "Enterprise"))
        self.assertEqual(w("WPA WPA-PSK TKIP"), ("WPA", "Personal"))
        self.assertEqual(w("Open"), ("Open", "Open"))
        self.assertEqual(w("WPA3 OWE AES-CCMP"), ("Enhanced Open (OWE)", "Open"))
        self.assertEqual(w("Unknown"), (None, None))
        self.assertEqual(w(None), (None, None))

    def test_channel_width_and_wps_version(self):
        self.assertEqual([hardware.channel_width(x) for x in ("HT20", "HT40+", "HT80", "HT160", "HT80+80", "", None)],
                         ["20 MHz", "40 MHz", "80 MHz", "160 MHz", "160 MHz", None, None])
        d11 = {"dot11.device.advertised_ssid_map": [{"dot11.advertisedssid.wps_version": 16,
                                                     "dot11.advertisedssid.wps_serial_number": "SECRET"}]}
        wps = hardware.wps_identity(d11)
        self.assertEqual(wps, {"version": "1.0"})
        self.assertNotIn("SECRET", str(wps))  # a serial number identifies one unit and is never read


class HelperTests(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(iso(1700000000, 191565), "2023-11-14T22:13:20.191Z")
        self.assertIsNone(iso(0))
        self.assertIsNone(iso(99999999999))
        self.assertIsNone(iso(None))

    def test_band_channel(self):
        self.assertEqual(band_channel(2412.0), ("2.4GHz", 1))
        self.assertEqual(band_channel(2484.0), ("2.4GHz", 14))
        self.assertEqual(band_channel(5180.0), ("5GHz", 36))
        self.assertEqual(band_channel(5955.0), ("6GHz", 1))
        self.assertEqual(band_channel(None), (None, None))


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls._tmp.name) / "synthetic-test.kismet"
        synthetic.build(cls.path)
        cls.hash_before = sha(cls.path)
        cls.db = KismetDb(str(cls.path))
        cls.sha = file_sha256(str(cls.path), use_cache=False)
        rejects = RejectLog(None, cls.path.name, "test")
        cls.stats = new_stats(rejects)
        opts = Options(quiet=True)
        cls.docs = [(idx, i, rec, d) for idx, i, rec, d in iter_documents(cls.db, cls.sha, "run-1", opts, cls.stats)]
        cls.by_rec = {}
        for idx, i, rec, d in cls.docs:
            cls.by_rec.setdefault(rec, []).append(d)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls._tmp.cleanup()

    def test_source_file_never_modified(self):
        self.assertEqual(sha(self.path), self.hash_before)

    def test_every_source_row_becomes_one_document(self):
        for table, records in (("packets", ["wifi.packet"]), ("devices", ["device"]),
                               ("alerts", ["alert"]), ("messages", ["message"]), ("datasources", ["datasource"])):
            n = sum(len(self.by_rec.get(r, [])) for r in records)
            self.assertEqual(n, self.db.count(table), table)
        self.assertEqual(len(self.by_rec["adsb.frame"]) + len(self.by_rec["data.event"]), self.db.count("data"))
        snaps = sum(len(self.by_rec.get(r, [])) for r in self.by_rec if r.startswith(("gps.snapshot", "snapshot.")))
        self.assertEqual(snaps, self.db.count("snapshots"))

    def test_deterministic_unique_ids(self):
        ids = [i for _, i, _, _ in self.docs]
        self.assertEqual(len(ids), len(set(ids)), "duplicate _id within one file")
        again = [i for _, i, _, _ in iter_documents(self.db, self.sha, "run-2", Options(quiet=True),
                                                   new_stats(RejectLog(None, "x", "y")))]
        self.assertEqual(ids, again, "ids must not depend on the run")

    def test_provenance_on_every_document(self):
        for _, _, _, d in self.docs:
            k = d["kismet"]
            self.assertEqual(k["source"]["file"], self.path.name)
            self.assertEqual(k["source"]["file_hash"], self.sha)
            self.assertEqual(k["version"], "2025.09.0")
            self.assertIn(k["source"]["table"], ("packets", "devices", "data", "alerts", "messages", "snapshots", "datasources"))

    def test_packet_coordinates_match_source_and_are_not_swapped(self):
        rows = {r[0]: r for r in self.db.con.execute("select rowid, lat, lon, ts_sec, signal, frequency from packets")}
        checked = 0
        for d in self.by_rec["wifi.packet"]:
            rid = d["kismet"]["source"]["rowid"]
            _, lat, lon, ts, sig, freq = rows[rid]
            loc = d["geo"].get("location")
            if lat == 0 and lon == 0:
                self.assertIsNone(loc)
                self.assertEqual(d["geo"]["status"], "no_fix")
            elif abs(lat) > 90:
                self.assertIsNone(loc)
                self.assertEqual(d["geo"]["status"], "invalid")
            else:
                self.assertEqual((loc["lat"], loc["lon"]), (lat, lon))
                self.assertEqual(d["geo"]["location_type"], "observer")
                self.assertTrue(-90 <= loc["lat"] <= 90 and 39.9 < loc["lat"] < 40.4 and -100.1 < loc["lon"] < -99.7)
                checked += 1
            self.assertTrue(d["@timestamp"].startswith(iso(ts)[:19]))
            self.assertEqual(d["wifi"]["frequency"], freq / 1000.0)  # kHz -> MHz
        self.assertGreater(checked, 50)

    def test_records_without_gps_get_no_coordinates(self):
        nofix = [d for d in self.by_rec["wifi.packet"] if d["geo"]["status"] == "no_fix"]
        self.assertGreater(len(nofix), 10)
        for d in nofix:
            self.assertNotIn("location", d["geo"])
            self.assertNotIn("gps", d)

    def test_signal_zero_is_absent_but_preserved(self):
        zero = [d for d in self.by_rec["wifi.packet"] if d["kismet"]["packet"]["signal_raw"] == 0]
        self.assertGreater(len(zero), 5)
        for d in zero:
            self.assertNotIn("rssi", d["wifi"])
        real = [d for d in self.by_rec["wifi.packet"] if d["kismet"]["packet"]["signal_raw"] < 0]
        for d in real:
            self.assertEqual(d["wifi"]["rssi"], float(d["kismet"]["packet"]["signal_raw"]))

    def test_multiple_observations_of_one_device_differ_in_time_and_signal(self):
        by_mac = {}
        for d in self.by_rec["wifi.packet"]:
            by_mac.setdefault(d["wifi"]["mac"], []).append(d)
        many = [v for v in by_mac.values() if len(v) >= 3]
        self.assertTrue(many, "expected a device with several observations")
        obs = many[0]
        self.assertGreater(len({d["@timestamp"] for d in obs}), 1)
        self.assertEqual(len({d["_id"] if "_id" in d else d["kismet"]["source"]["rowid"] for d in obs}), len(obs))

    def test_packet_device_link_is_marked_derived(self):
        linked = [d for d in self.by_rec["wifi.packet"] if d["kismet"].get("device_key")]
        self.assertTrue(linked)
        for d in linked:
            self.assertIn("kismet.device_key", d["kismet"]["derived"])
            if "bssid" in d["wifi"]:
                self.assertIn("wifi.bssid", d["kismet"]["derived"])

    def test_wifi_devices_ssid_and_position_semantics(self):
        devs = self.by_rec["device"]
        aps = [d for d in devs if d["kismet"]["device"].get("type") == "Wi-Fi AP"]
        self.assertTrue(any(d.get("wifi", {}).get("ssid") for d in aps), "an AP with an SSID")
        self.assertTrue(any(d.get("wifi", {}).get("ssid_hidden") for d in aps), "a hidden-SSID AP")
        for d in devs:
            if d["kismet"]["phy"] == "IEEE802.11" and "location" in d["geo"]:
                self.assertEqual(d["geo"]["location_type"], "observer_centroid")  # never claimed to be the AP's position

    def test_garbage_kismet_centroid_is_rejected(self):
        bad = [d for d in self.by_rec["device"] if d["geo"].get("reason") == "centroid_outside_device_bounds"]
        self.assertGreaterEqual(len(bad), 1)
        for d in bad:
            self.assertNotIn("location", d["geo"])
            self.assertEqual(d["geo"]["status"], "invalid")
            # the good position survives exactly when the source has one (some devices have no peak_loc at all)
            sig = d["kismet"]["raw"].get("kismet.device.base.signal") or {}
            src_peak = (sig.get("kismet.common.signal.peak_loc") or {}).get("kismet.common.location.geopoint")
            has_peak = geo.from_kismet_geopoint(src_peak)[0] == geo.OK
            self.assertEqual("peak_signal_location" in d["geo"], has_peak)

    def test_aircraft_positions_are_emitter_reported_and_sentinels_dropped(self):
        air = [d for d in self.by_rec["device"] if d["kismet"]["phy"] == "ADSB"]
        with_pos = [d for d in air if "location" in d["geo"]]
        no_pos = [d for d in air if "location" not in d["geo"]]
        self.assertTrue(with_pos and no_pos)
        for d in with_pos:
            self.assertEqual(d["geo"]["location_type"], "emitter_reported")
            self.assertIn("icao", d["aircraft"])
        for d in no_pos:
            self.assertEqual(d["geo"]["status"], "no_fix")
        frames = self.by_rec["adsb.frame"]
        self.assertTrue(any("location" in f["geo"] for f in frames) and any("location" not in f["geo"] for f in frames))
        for f in frames:
            if "location" in f["geo"]:
                self.assertEqual(f["geo"]["location_type"], "emitter_reported")
                self.assertNotIn("gps", f)  # an aircraft position is not the collector's GPS

    def test_malformed_and_unknown_records_are_reported_not_silently_dropped(self):
        bad_dev = [d for d in self.by_rec["device"] if d["kismet"].get("parse_error")]
        self.assertEqual(len(bad_dev), 1)
        self.assertNotIn("raw", bad_dev[0]["kismet"])
        self.assertEqual(bad_dev[0]["kismet"]["device_key"], "SYNTH_BAD_JSON")
        self.assertGreaterEqual(self.stats["devices"].parse_errors, 1)
        self.assertGreaterEqual(self.stats["data"].parse_errors, 1)
        unknown = self.by_rec["data.event"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["kismet"]["data"]["type"], "FOO")
        self.assertEqual(unknown[0]["geo"]["location_type"], "unspecified")  # meaning of the position is not asserted
        weird = self.by_rec.get("snapshot.weird", [])
        self.assertEqual(len(weird), 1)
        self.assertGreaterEqual(self.stats["packets"].invalid_gps, 1)

    def test_adsb_decode_errors_are_rejected_but_the_source_value_is_kept(self):
        frames = {f["kismet"]["devmac"]: f for f in self.by_rec["adsb.frame"] if f["kismet"].get("devmac", "").startswith("AA:BB:CC:00:01")}
        bad_lat, far = frames["AA:BB:CC:00:01:01"], frames["AA:BB:CC:00:01:02"]
        self.assertNotIn("location", bad_lat["geo"])
        self.assertEqual(bad_lat["geo"]["status"], "invalid")
        self.assertEqual(bad_lat["geo"]["rejected"], {"lat": 112.2495, "lon": -146.1182})  # not silently discarded
        # a legal coordinate, but thousands of km from where the collector ever was in this capture
        self.assertNotIn("location", far["geo"])
        self.assertEqual(far["geo"]["reason"], "implausible_range_from_collector")
        self.assertEqual(far["geo"]["rejected"], {"lat": 2.5318, "lon": 2.3331})
        # ...while real aircraft (hundreds of km away at most) are untouched
        real = [f for f in self.by_rec["adsb.frame"] if "location" in f["geo"]]
        self.assertGreater(len(real), 5)

    def test_absurd_aircraft_altitude_is_not_indexed_but_kept(self):
        f = [x for x in self.by_rec["adsb.frame"] if x["kismet"].get("devmac") == "AA:BB:CC:00:01:09"][0]
        self.assertEqual(f["geo"]["location"], {"lat": 40.05, "lon": -99.85})  # the position is fine
        self.assertNotIn("altitude", f["aircraft"])
        self.assertAlmostEqual(f["aircraft"]["rejected"]["altitude"], 5.622567663155806e18)
        for _, _, rec, d in self.docs:  # and no plausible-looking altitude is ever outside the band
            alt = (d.get("aircraft") or {}).get("altitude")
            self.assertTrue(alt is None or -1500 <= alt <= 30000)

    def test_hardware_fields_are_derived_from_the_stored_json(self):
        devs = self.by_rec["device"]
        with_brand = [d for d in devs if d.get("device", {}).get("manufacturer")]
        self.assertTrue(with_brand, "some device has a brand")
        for d in devs:
            dev = d.get("device") or {}
            raw = d["kismet"]["raw"] if d["kismet"].get("raw") else {}
            if dev.get("manufacturer"):
                self.assertIn("device.manufacturer", d["kismet"]["derived"])
                self.assertNotIn(dev["manufacturer"].lower(), ("unknown", "adsb"))
            if dev.get("model", {}).get("name"):
                self.assertIn("device.model.name", d["kismet"]["derived"])
            wifi = d.get("wifi") or {}
            if wifi.get("security"):  # only for access points, and consistent with Kismet's own crypt string
                self.assertEqual(d["kismet"]["device"]["type"], "Wi-Fi AP")
                self.assertEqual((wifi["security"], wifi["auth"]), hardware.wifi_security(wifi["crypt"]))
            if d["kismet"]["device"].get("type") != "Wi-Fi AP":
                for f in ("security", "auth", "channel_width"):
                    self.assertNotIn(f, wifi)
            for v in (dev.get("model", {}).get("name"), dev.get("manufacturer")):
                self.assertNotIn("serial", str(v).lower())
            self.assertNotIn("wps_serial_number", str({k: v for k, v in d.items() if k != "kismet"}))

    def test_brand_model_and_version_of_known_devices(self):
        aps = {d["wifi"]["mac"]: d for d in self.by_rec["device"] if d.get("wifi", {}).get("mac", "").startswith("02:AA:")}
        a = aps["02:AA:00:00:00:01"]                       # OUI name "Acme Wireless Co.,Ltd." + WPS model
        self.assertEqual(a["device"]["manufacturer"], "Acme Wireless")
        self.assertEqual(a["device"]["model"], {"name": "AW-100", "identifier": "1.2"})
        self.assertEqual(a["wifi"]["wps"], {"version": "1.0", "device_name": "Acme Gateway"})
        self.assertEqual((a["wifi"]["security"], a["wifi"]["auth"], a["wifi"]["channel_width"]),
                         ("WPA2", "Personal", "20 MHz"))
        z = aps["02:AA:00:00:00:02"]                       # WPA3 on an 80 MHz channel, no WPS
        self.assertEqual((z["wifi"]["security"], z["wifi"]["channel_width"]), ("WPA3", "80 MHz"))
        self.assertNotIn("model", z["device"])
        f = aps["02:AA:00:00:00:04"]                       # OUI unknown -> the maker it announced in WPS
        self.assertEqual(f["device"]["manufacturer"], "Borealis Devices")
        self.assertEqual((f["wifi"]["security"], f["wifi"]["wps"]["version"]), ("Open", "2.0"))
        self.assertEqual(aps["02:AA:00:00:00:06"]["wifi"]["security"], "WPA2/WPA3")
        self.assertEqual(aps["02:AA:00:00:00:05"]["wifi"]["auth"], "Enterprise")
        self.assertEqual(aps["02:AA:00:00:00:08"]["device"]["manufacturer"], "Zephyr Networks")  # SHOUTED OUI name merged
        # the WPS serial number identifies one unit: it stays only in the stored original JSON
        indexed = {k: v for k, v in a.items() if k != "kismet"}
        self.assertNotIn("SN-DO-NOT-INDEX", str(indexed))
        planes = {d["aircraft"]["icao"]: d for d in self.by_rec["device"] if "aircraft" in d}
        self.assertEqual(planes["A00001"]["device"], {"manufacturer": "Skyworks", "model": {"name": "SW-100"}})
        self.assertEqual(planes["A00004"]["device"]["manufacturer"], "Fiction Aerospace Canada")  # corporate suffix "LP" cut
        self.assertNotIn("device", planes["A00007"])       # "Unknown" is not a brand

    def test_original_kismet_json_preserved_for_devices(self):
        for d in self.by_rec["device"]:
            if not d["kismet"].get("parse_error"):
                self.assertIn("kismet.device.base.key", d["kismet"]["raw"])
                self.assertEqual(d["kismet"]["raw"]["kismet.device.base.key"], d["kismet"]["device_key"])

    def test_no_zero_zero_point_anywhere(self):
        for _, _, rec, d in self.docs:
            for f in ("location", "peak_signal_location"):
                p = d.get("geo", {}).get(f)
                if p:
                    self.assertFalse(p["lat"] == 0 and p["lon"] == 0, rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
