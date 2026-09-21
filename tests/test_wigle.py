import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kismet_cartographer import wigle  # noqa: E402


class AuthMapping(unittest.TestCase):
    def test_common(self):
        cases = {
            "Open": "[ESS]", "": "[ESS]",
            "WPA2 WPA2-PSK AES-CCMP": "[WPA2-PSK-CCMP][ESS]",
            "WPA2 WPA2-PSK TKIP AES-CCMP": "[WPA2-PSK-TKIP+CCMP][ESS]",
            "WPA2 WPA2-EAP AES-CCMP": "[WPA2-EAP-CCMP][ESS]",
            "WPA3 WPA3-SAE AES-CCMP": "[WPA3-SAE-CCMP][ESS]",
            "WPA WPA-PSK TKIP": "[WPA-PSK-TKIP][ESS]",
            "WPA1 WPA1-PSK TKIP": "[WPA-PSK-TKIP][ESS]",
            "WPA2 AES-CCMP": "[WPA2-CCMP][ESS]",
            "WEP40": "[WEP][ESS]",
        }
        for kismet, expect in cases.items():
            self.assertEqual(wigle.wigle_auth(kismet), (expect, True), kismet)

    def test_unknown_is_reported(self):
        self.assertEqual(wigle.wigle_auth("AES-BIP-CMAC256"), ("[ESS]", False))


class Format(unittest.TestCase):
    def test_timestamp_utc(self):
        self.assertEqual(wigle._ts(1700000000), "2023-11-14 22:13:20")

    def test_channel_from_frequency(self):
        self.assertEqual(wigle._channel(None, 5680000), 136)
        self.assertEqual(wigle._channel("11", None), 11)
        self.assertIsNone(wigle._channel(None, None))

    def test_header(self):
        self.assertTrue(wigle.header_line(["2025.09.0"]).startswith("WigleWifi-1.4,appRelease="))


if __name__ == "__main__":
    unittest.main()
