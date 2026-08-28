import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ibkr_note4 import cli as DASHBOARD


class DashboardTests(unittest.TestCase):
    def test_sample_normalizes_and_renders_exact_size(self):
        payload = json.loads(DASHBOARD.SAMPLE_SNAPSHOT.read_text())
        snapshot = DASHBOARD.normalize_snapshot(payload, "test")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            DASHBOARD.render(snapshot, output, snapshot["history"])
            with DASHBOARD.Image.open(output) as image:
                self.assertEqual(image.size, (400, 300))
                self.assertEqual(image.mode, "1")

    def test_manifest_data_does_not_include_account_id(self):
        payload = {
            "currency": "USD",
            "net_liquidation": 100,
            "positions": [{"symbol": "ABC", "position": 2, "marketValue": 100}],
        }
        snapshot = DASHBOARD.normalize_snapshot(payload, "test")
        self.assertNotIn("account_id", snapshot)
        self.assertEqual(snapshot["positions"][0]["symbol"], "ABC")

    def test_flex_xml_maps_positions_without_persisting_credentials(self):
        report = b'''<FlexQueryResponse><FlexStatements><FlexStatement currency="USD">
        <NetAssetValue endingValue="12345.67" currency="USD" />
        <OpenPositions><OpenPosition symbol="XYZ" position="3" markPrice="12.50"
        positionValue="37.50" fifoPnlUnrealized="4.25" /></OpenPositions>
        </FlexStatement></FlexStatements></FlexQueryResponse>'''
        snapshot = DASHBOARD.parse_flex_xml(report)
        self.assertEqual(snapshot["net_liquidation"], 12345.67)
        self.assertEqual(snapshot["positions"][0]["symbol"], "XYZ")
        self.assertEqual(snapshot["positions"][0]["market_price"], 12.5)

    def test_money_sign_precedes_currency_symbol(self):
        self.assertEqual(DASHBOARD.money(-91.25, "USD", signed=True), "-$91.25")

    def test_nav_axis_numbers_do_not_repeat_currency(self):
        self.assertEqual(DASHBOARD.axis_number(128_500), "128.5K")
        self.assertNotIn("$", DASHBOARD.axis_number(128_500))

    def test_day_percent_is_derived_when_source_omits_it(self):
        payload = {
            "net_liquidation": 102,
            "daily_pnl": 2,
            "positions": [{"symbol": "ABC", "market_value": 52, "daily_pnl": 2}],
        }
        snapshot = DASHBOARD.normalize_snapshot(payload, "test")
        self.assertAlmostEqual(snapshot["daily_pnl_pct"], 2.0)
        self.assertAlmostEqual(snapshot["positions"][0]["daily_pct"], 4.0)

    def test_display_timestamp_is_converted_to_utc_plus_eight(self):
        self.assertEqual(
            DASHBOARD.display_timestamp("2026-08-28T08:00:00Z"),
            "UTC+8 08/28 16:00",
        )

    def test_nav_chart_has_full_frame_and_lower_table(self):
        payload = json.loads(DASHBOARD.SAMPLE_SNAPSHOT.read_text())
        snapshot = DASHBOARD.normalize_snapshot(payload, "test")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            DASHBOARD.render(snapshot, output, snapshot["history"])
            with DASHBOARD.Image.open(output) as image:
                self.assertEqual(image.getpixel((250, 54)), 0)
                self.assertEqual(image.getpixel((390, 142)), 0)
                self.assertEqual(image.getpixel((10, 150)), 0)

    def test_device_identifier_is_masked(self):
        masked = DASHBOARD.masked_identifier("AA:BB:CC:DD:EE:FF")
        self.assertNotIn("AABB", masked)
        self.assertTrue(masked.endswith("EEFF"))

    def test_verified_ssl_context_is_enabled(self):
        context = DASHBOARD.verified_ssl_context()
        self.assertEqual(context.verify_mode, DASHBOARD.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_json_snapshot_can_be_read_from_stdin(self):
        payload = {"source": "ibkr", "currency": "USD", "net_liquidation": 42, "positions": []}
        previous_stdin = DASHBOARD.sys.stdin
        try:
            DASHBOARD.sys.stdin = io.StringIO(json.dumps(payload))
            snapshot = DASHBOARD.load_json_source("-")
        finally:
            DASHBOARD.sys.stdin = previous_stdin
        self.assertEqual(snapshot["net_liquidation"], 42)
        self.assertEqual(snapshot["source"], "ibkr")

    def test_history_keeps_one_nav_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            first = {"as_of": "2026-08-28T09:00:00+08:00", "net_liquidation": 100}
            second = {"as_of": "2026-08-28T16:00:00+08:00", "net_liquidation": 105}
            next_day = {"as_of": "2026-08-29T16:00:00+08:00", "net_liquidation": 103}
            DASHBOARD.append_history(first, history_path)
            self.assertEqual(DASHBOARD.append_history(second, history_path), [105.0])
            self.assertEqual(DASHBOARD.append_history(next_day, history_path), [105.0, 103.0])


if __name__ == "__main__":
    unittest.main()
