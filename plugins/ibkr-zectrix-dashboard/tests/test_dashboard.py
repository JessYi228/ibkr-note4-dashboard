import importlib.util
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "ibkr-zectrix-dashboard"
SCRIPT = SKILL_ROOT / "scripts" / "ibkr_zectrix_dashboard.py"
SAMPLE = SKILL_ROOT / "assets" / "sample_snapshot.json"
SPEC = importlib.util.spec_from_file_location("dashboard", SCRIPT)
DASHBOARD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DASHBOARD)


class DashboardTests(unittest.TestCase):
    def test_preferences_store_only_non_secret_choices_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(path)}, clear=True):
                DASHBOARD.save_preferences(
                    {
                        "data_source": "codex_ibkr",
                        "secret_backend": "environment",
                        "timezone": "America/New_York",
                    }
                )
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["data_source"], "codex_ibkr")
                self.assertNotIn("ZECTRIX_API_KEY", saved)
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_preferences_reject_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with self.assertRaises(DASHBOARD.DashboardError):
                DASHBOARD.save_preferences({"ZECTRIX_API_KEY": "must-not-be-written"}, path)

    def test_authorization_is_persisted_and_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(path)}, clear=True):
                with patch("builtins.input", return_value="yes"):
                    DASHBOARD.command_authorize(Namespace(check=False, revoke=False))
                self.assertIs(DASHBOARD.load_preferences()["delivery_authorized"], True)
                DASHBOARD.command_authorize(Namespace(check=True, revoke=False))
                DASHBOARD.command_authorize(Namespace(check=False, revoke=True))
                self.assertIs(DASHBOARD.load_preferences()["delivery_authorized"], False)
                with self.assertRaisesRegex(DASHBOARD.DashboardError, "not authorized"):
                    DASHBOARD.command_authorize(Namespace(check=True, revoke=False))

    def test_every_push_fails_before_secret_access_without_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(path)}, clear=True):
                with patch.object(DASHBOARD, "secret", side_effect=AssertionError("secret accessed")):
                    with self.assertRaisesRegex(DASHBOARD.DashboardError, "not authorized"):
                        DASHBOARD.push_zectrix(Path(directory) / "preview.png")

    def test_run_and_direct_push_use_the_same_authorization_guard(self):
        snapshot = {"source": "json", "currency": "USD", "positions": []}
        with tempfile.TemporaryDirectory() as directory:
            preferences = Path(directory) / "preferences.json"
            output = Path(directory) / "preview.png"
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(preferences)}, clear=True):
                direct = DASHBOARD.build_parser().parse_args(["push", str(output)])
                with self.assertRaisesRegex(DASHBOARD.DashboardError, "not authorized"):
                    direct.handler(direct)
                with (
                    patch.object(DASHBOARD, "fetch_snapshot", return_value=snapshot),
                    patch.object(DASHBOARD, "append_history", return_value=[]),
                    patch.object(DASHBOARD, "render"),
                    self.assertRaisesRegex(DASHBOARD.DashboardError, "not authorized"),
                ):
                    DASHBOARD.command_run(Namespace(output=str(output), no_push=False))

    def test_environment_overrides_remembered_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(
                os.environ,
                {
                    "IBKR_ZECTRIX_PREFERENCES_PATH": str(path),
                    "DASHBOARD_CURRENCY": "CAD",
                },
                clear=True,
            ):
                DASHBOARD.save_preferences({"currency": "USD"})
                self.assertEqual(DASHBOARD.env("DASHBOARD_CURRENCY", "EUR"), "CAD")

    def test_environment_secret_backend_does_not_fall_back_to_keychain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(path)}, clear=True):
                DASHBOARD.save_preferences({"secret_backend": "environment"})
                original = DASHBOARD.keychain_secret
                try:
                    DASHBOARD.keychain_secret = lambda _service: "keychain-value"
                    self.assertEqual(DASHBOARD.secret("ZECTRIX_API_KEY", "service"), "")
                finally:
                    DASHBOARD.keychain_secret = original

    def test_configure_remembers_choices_without_requesting_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            args = Namespace(
                source="flex",
                secret_backend="environment",
                timezone="America/New_York",
                currency="usd",
                page_id="1",
                max_positions=4,
            )
            with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(path)}, clear=True):
                output = io.StringIO()
                with redirect_stdout(output):
                    DASHBOARD.command_configure(args)
                saved_text = path.read_text(encoding="utf-8")
                self.assertNotIn("ZECTRIX_API_KEY=", saved_text)
                self.assertNotIn("IBKR_FLEX_TOKEN=", saved_text)
                self.assertIn("IBKR_FLEX_TOKEN", output.getvalue())

    def test_settings_reports_presence_without_printing_secret_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.dict(
                os.environ,
                {
                    "IBKR_ZECTRIX_PREFERENCES_PATH": str(path),
                    "ZECTRIX_API_KEY": "not-for-output",
                },
                clear=True,
            ):
                DASHBOARD.save_preferences({"data_source": "codex_ibkr", "secret_backend": "environment"})
                output = io.StringIO()
                with redirect_stdout(output):
                    DASHBOARD.command_settings(Namespace(json=True))
                rendered = output.getvalue()
                self.assertIn('"zectrix_api_key": "present"', rendered)
                self.assertNotIn("not-for-output", rendered)

    def test_sample_normalizes_and_renders_exact_size(self):
        payload = json.loads(SAMPLE.read_text())
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

    def test_display_timestamp_uses_configured_timezone_offset(self):
        self.assertEqual(
            DASHBOARD.display_timestamp("2026-08-28T08:00:00Z", "America/New_York"),
            "UTC-4 08/28 04:00",
        )

    def test_small_eight_uses_unambiguous_pixel_glyph(self):
        image = DASHBOARD.Image.new("L", (20, 20), 255)
        draw = DASHBOARD.ImageDraw.Draw(image)
        DASHBOARD.draw_eink_text(draw, (0, 0), "8", font=DASHBOARD.load_font(10), fill=0)
        black = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if image.getpixel((x, y)) == 0
        ]
        left = min(x for x, _ in black)
        top = min(y for _, y in black)
        right = max(x for x, _ in black)
        bottom = max(y for _, y in black)
        rows = tuple(
            "".join("1" if image.getpixel((x, y)) == 0 else "0" for x in range(left, right + 1))
            for y in range(top, bottom + 1)
        )
        self.assertEqual(rows, DASHBOARD.PIXEL_DIGITS["8"])
        self.assertEqual(rows[3][0], "0")

    def test_nav_chart_has_full_frame_and_lower_table(self):
        payload = json.loads(SAMPLE.read_text())
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

    def test_relay_rejects_any_source_other_than_codex_ibkr(self):
        payload = {"source": "json", "currency": "USD", "net_liquidation": 42, "positions": []}
        previous_stdin = DASHBOARD.sys.stdin
        try:
            DASHBOARD.sys.stdin = io.StringIO(json.dumps(payload))
            with tempfile.TemporaryDirectory() as directory:
                args = Namespace(
                    input="-",
                    output=str(Path(directory) / "preview.png"),
                    push=False,
                    dedupe_state=str(Path(directory) / "last-push.json"),
                    force=False,
                )
                with self.assertRaises(DASHBOARD.DashboardError):
                    DASHBOARD.command_relay(args)
        finally:
            DASHBOARD.sys.stdin = previous_stdin

    def test_relay_deduplicates_without_persisting_snapshot_data(self):
        payload = {
            "source": "codex_ibkr",
            "as_of": "2026-08-28T12:00:00Z",
            "currency": "USD",
            "net_liquidation": 100,
            "cash": 10,
            "buying_power": 20,
            "positions": [{"symbol": "ABC", "quantity": 1, "market_price": 90, "market_value": 90}],
        }
        pushes = []
        previous_push = DASHBOARD.push_zectrix
        previous_stdin = DASHBOARD.sys.stdin
        try:
            DASHBOARD.push_zectrix = lambda path: pushes.append(path)
            with tempfile.TemporaryDirectory() as directory:
                preferences = Path(directory) / "preferences.json"
                output = Path(directory) / "preview.png"
                state = Path(directory) / "last-push.json"
                args = Namespace(
                    input="-",
                    output=str(output),
                    push=True,
                    dedupe_state=str(state),
                    force=False,
                )
                with patch.dict(os.environ, {"IBKR_ZECTRIX_PREFERENCES_PATH": str(preferences)}, clear=True):
                    DASHBOARD.save_preferences({"delivery_authorized": True})
                    DASHBOARD.sys.stdin = io.StringIO(json.dumps(payload))
                    DASHBOARD.command_relay(args)
                    payload["as_of"] = "2026-08-28T12:30:00Z"
                    DASHBOARD.sys.stdin = io.StringIO(json.dumps(payload))
                    DASHBOARD.command_relay(args)
                    DASHBOARD.save_preferences({"delivery_authorized": False})
                    DASHBOARD.sys.stdin = io.StringIO(json.dumps(payload))
                    with self.assertRaisesRegex(DASHBOARD.DashboardError, "not authorized"):
                        DASHBOARD.command_relay(args)
                self.assertEqual(len(pushes), 1)
                saved = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual(set(saved), {"sha256", "pushed_at"})
                self.assertNotIn("ABC", state.read_text(encoding="utf-8"))
                if os.name == "posix":
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        finally:
            DASHBOARD.push_zectrix = previous_push
            DASHBOARD.sys.stdin = previous_stdin

    def test_history_and_dashboard_files_are_private(self):
        payload = json.loads(SAMPLE.read_text())
        snapshot = DASHBOARD.normalize_snapshot(payload, "sample")
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "state" / "history.json"
            output_path = Path(directory) / "output" / "dashboard.png"
            DASHBOARD.append_history(snapshot, history_path)
            DASHBOARD.render(snapshot, output_path, snapshot["history"])
            if os.name == "posix":
                self.assertEqual(history_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(history_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(output_path.parent.stat().st_mode & 0o777, 0o700)

    def test_public_skill_bundle_contains_every_runtime_resource(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SAMPLE.is_file())
        self.assertTrue((SKILL_ROOT / "requirements.txt").is_file())
        self.assertTrue((SKILL_ROOT / "references" / "runtime.md").is_file())

    def test_public_listing_assets_and_legal_documents_exist(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        interface = manifest["interface"]
        for field in ("logo", "composerIcon"):
            self.assertTrue((ROOT / interface[field].removeprefix("./")).is_file())
        self.assertNotIn("screenshots", interface)
        self.assertTrue((ROOT / "assets" / "dashboard-preview.png").is_file())
        self.assertTrue((ROOT / "PRIVACY.md").is_file())
        self.assertTrue((ROOT / "TERMS.md").is_file())
        self.assertTrue((ROOT / "SUBMISSION.md").is_file())
        self.assertIn("/TERMS.md", interface["termsOfServiceURL"])


if __name__ == "__main__":
    unittest.main()
