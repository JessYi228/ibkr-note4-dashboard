import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        report = b'''<FlexQueryResponse><FlexStatements><FlexStatement currency="USD"
        fromDate="20260801" toDate="20260827" period="Last30CalendarDays">
        <EquitySummaryByReportDateInBase>
          <EquitySummaryByReportDateInBase reportDate="20260826" cash="100.00" total="12000.00" />
          <EquitySummaryByReportDateInBase reportDate="20260827" cash="125.50" total="12345.67" />
        </EquitySummaryByReportDateInBase>
        <ChangeInNAV fromDate="20260801" toDate="20260827" markToMarket="999.00" endingValue="12345.67" />
        <OpenPositions><OpenPosition symbol="XYZ" position="3" markPrice="12.50"
        positionValue="37.50" fifoPnlUnrealized="4.25" /></OpenPositions>
        </FlexStatement></FlexStatements></FlexQueryResponse>'''
        snapshot = DASHBOARD.parse_flex_xml(report)
        self.assertEqual(snapshot["net_liquidation"], 12345.67)
        self.assertEqual(snapshot["cash"], 125.5)
        self.assertEqual(snapshot["as_of"], "2026-08-27")
        self.assertEqual(snapshot["nav_history"], [12000.0, 12345.67])
        self.assertIsNone(snapshot["daily_pnl"])
        self.assertIsNone(snapshot["buying_power"])
        self.assertEqual(snapshot["positions"][0]["symbol"], "XYZ")
        self.assertEqual(snapshot["positions"][0]["market_price"], 12.5)
        self.assertIsNone(snapshot["positions"][0]["daily_pnl"])

    def test_flex_last_business_day_maps_true_daily_pnl(self):
        report = b'''<FlexQueryResponse><FlexStatements><FlexStatement currency="USD"
        fromDate="20260827" toDate="20260827" period="LastBusinessDay">
        <EquitySummaryByReportDateInBase reportDate="20260827" cash="125.50" total="12345.67" />
        <ChangeInNAV fromDate="20260827" toDate="20260827" markToMarket="42.25" endingValue="12345.67" />
        <OpenPosition symbol="XYZ" position="3" markPrice="12.50" positionValue="37.50"
        fifoPnlUnrealized="4.25" />
        <MTMPerformanceSummaryInBase symbol="XYZ" total="2.75" />
        </FlexStatement></FlexStatements></FlexQueryResponse>'''
        snapshot = DASHBOARD.parse_flex_xml(report)
        self.assertEqual(snapshot["daily_pnl"], 42.25)
        self.assertEqual(snapshot["positions"][0]["daily_pnl"], 2.75)
        self.assertNotEqual(snapshot["positions"][0]["daily_pnl"], snapshot["positions"][0]["unrealized_pnl"])

    def test_flex_error_and_pending_responses_fail_closed(self):
        failed = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1003</ErrorCode><ErrorMessage>Invalid query</ErrorMessage></FlexStatementResponse>"
        pending = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress</ErrorMessage></FlexStatementResponse>"
        with self.assertRaises(DASHBOARD.DashboardError):
            DASHBOARD.parse_flex_xml(failed)
        self.assertTrue(DASHBOARD.flex_report_is_pending(pending))

    def test_flex_multiple_statements_are_rejected(self):
        report = b"<FlexQueryResponse><FlexStatements><FlexStatement/><FlexStatement/></FlexStatements></FlexQueryResponse>"
        with self.assertRaises(DASHBOARD.DashboardError):
            DASHBOARD.parse_flex_xml(report)

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
        self.assertEqual(DASHBOARD.display_timestamp("2026-08-27"), "AS OF 08/27")
        self.assertEqual(
            DASHBOARD.display_timestamp("2026-08-28T08:00:00Z", "America/New_York"),
            "UTC-4 08/28 04:00",
        )

    def test_small_eight_uses_unambiguous_pixel_glyph(self):
        image = DASHBOARD.Image.new("L", (20, 20), 255)
        draw = DASHBOARD.ImageDraw.Draw(image)
        DASHBOARD.draw_eink_text(draw, (0, 0), "8", font=DASHBOARD.load_font(10), fill=0)
        black = [(x, y) for y in range(image.height) for x in range(image.width) if image.getpixel((x, y)) == 0]
        left, top = min(x for x, _ in black), min(y for _, y in black)
        right, bottom = max(x for x, _ in black), max(y for _, y in black)
        rows = tuple(
            "".join("1" if image.getpixel((x, y)) == 0 else "0" for x in range(left, right + 1))
            for y in range(top, bottom + 1)
        )
        self.assertEqual(rows, DASHBOARD.PIXEL_DIGITS["8"])

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

    def test_live_json_mode_never_falls_back_to_sample(self):
        with mock.patch.dict(os.environ, {"IBKR_SOURCE": "json", "IBKR_JSON_SOURCE": ""}, clear=False):
            with self.assertRaises(DASHBOARD.DashboardError):
                DASHBOARD.fetch_snapshot()

    def test_remote_json_requires_https(self):
        with self.assertRaises(DASHBOARD.DashboardError):
            DASHBOARD.require_https_url("http://example.com/snapshot.json", "IBKR_JSON_SOURCE", allow_local_http=True)
        DASHBOARD.require_https_url("http://127.0.0.1/snapshot.json", "IBKR_JSON_SOURCE", allow_local_http=True)

    def test_unknown_dashboard_timezone_is_actionable(self):
        with self.assertRaises(DASHBOARD.DashboardError):
            DASHBOARD.dashboard_timezone("Not/A-Timezone")

    def test_missing_optional_values_render_as_na(self):
        payload = {"as_of": "2026-08-27", "currency": "USD", "net_liquidation": 42, "positions": []}
        snapshot = DASHBOARD.normalize_snapshot(payload, "flex")
        self.assertIsNone(snapshot["cash"])
        self.assertIsNone(snapshot["daily_pnl"])
        self.assertEqual(DASHBOARD.money(snapshot["cash"], "USD"), "N/A")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "na.png"
            DASHBOARD.render(snapshot, output, [])
            self.assertTrue(output.exists())

    def test_fingerprint_ignores_as_of_and_state_contains_only_hash(self):
        payload = {"as_of": "2026-08-27", "currency": "USD", "net_liquidation": 42, "positions": []}
        first = DASHBOARD.normalize_snapshot(payload, "flex")
        payload["as_of"] = "2026-08-28"
        second = DASHBOARD.normalize_snapshot(payload, "flex")
        fingerprint = DASHBOARD.snapshot_fingerprint(first, [41, 42])
        self.assertEqual(fingerprint, DASHBOARD.snapshot_fingerprint(second, [41, 42]))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "last-push.json"
            DASHBOARD.write_last_push_fingerprint(state, fingerprint)
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"sha256", "pushed_at"})
            self.assertEqual(saved["sha256"], fingerprint)

    def test_request_bytes_retries_transient_http_error(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"ok"

        transient = DASHBOARD.urllib.error.HTTPError("https://example.com", 503, "busy", {}, None)
        with mock.patch.object(DASHBOARD.urllib.request, "urlopen", side_effect=[transient, Response()]) as urlopen:
            with mock.patch.object(DASHBOARD.time, "sleep"):
                self.assertEqual(DASHBOARD.request_bytes("https://example.com", attempts=2), b"ok")
        self.assertEqual(urlopen.call_count, 2)

    def test_run_deduplicates_push_and_persists_no_snapshot(self):
        payload = json.loads(DASHBOARD.SAMPLE_SNAPSHOT.read_text())
        snapshot = DASHBOARD.normalize_snapshot(payload, "flex")
        args = DASHBOARD.argparse.Namespace(no_push=False, force=False, no_dedupe=False, output=None)
        pushes = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dashboard.png"
            history = Path(directory) / "history.json"
            dedupe = Path(directory) / "last-push.json"
            args.output = str(output)
            environment = {
                "DASHBOARD_STATE_PATH": str(history),
                "DASHBOARD_DEDUPE_STATE_PATH": str(dedupe),
                "HEALTHCHECK_URL": "",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(DASHBOARD, "fetch_snapshot", return_value=snapshot):
                    with mock.patch.object(DASHBOARD, "push_zectrix", side_effect=lambda path: pushes.append(path)):
                        DASHBOARD.command_run(args)
                        DASHBOARD.command_run(args)
            self.assertEqual(len(pushes), 1)
            saved_text = dedupe.read_text(encoding="utf-8")
            self.assertNotIn("AAPL", saved_text)
            self.assertEqual(set(json.loads(saved_text)), {"sha256", "pushed_at"})

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
