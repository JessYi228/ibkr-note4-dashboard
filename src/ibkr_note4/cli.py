#!/usr/bin/env python3
"""Read-only IBKR portfolio snapshot renderer and ZECTRIX NOTE4 delivery client."""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


WIDTH = 400
HEIGHT = 300
USER_AGENT = "ibkr-note4-dashboard/0.1"
ZECTRIX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/zectrix-api-key"
IBKR_FLEX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/ibkr-flex-token"
PACKAGE_ROOT = Path(__file__).resolve().parent
SAMPLE_SNAPSHOT = PACKAGE_ROOT / "assets" / "sample_snapshot.json"

ENV_TEMPLATE = """# Choose: json, flex, or client_portal
IBKR_SOURCE=json
IBKR_JSON_SOURCE=

# IBKR Flex Web Service
IBKR_FLEX_QUERY_ID=
IBKR_FLEX_TOKEN=
IBKR_FLEX_BASE_URL=https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService

# IBKR Client Portal Gateway
IBKR_CP_BASE_URL=https://localhost:5000/v1/api
IBKR_CP_ACCOUNT_ID=
IBKR_CP_VERIFY_TLS=false

# ZECTRIX Open API
ZECTRIX_API_BASE_URL=https://cloud.zectrix.com
ZECTRIX_API_KEY=
ZECTRIX_DEVICE_ID=
ZECTRIX_PAGE_ID=1

# Dashboard
DASHBOARD_CURRENCY=USD
DASHBOARD_TIMEZONE=Asia/Shanghai
DASHBOARD_STATE_PATH=state/history.json
DASHBOARD_OUTPUT_PATH=output/ibkr-dashboard.png
DASHBOARD_MAX_POSITIONS=4
"""


class DashboardError(RuntimeError):
    """Actionable dashboard error with secret-safe text."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_env_file(path: str) -> None:
    """Load a small KEY=VALUE file without overriding the process environment."""
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def keychain_secret(service: str) -> str:
    """Read a macOS Keychain item without logging its value."""
    security = Path("/usr/bin/security")
    if sys.platform != "darwin" or not security.exists():
        return ""
    try:
        result = subprocess.run(
            [str(security), "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def secret(env_name: str, keychain_service: str) -> str:
    return env(env_name) or keychain_secret(keychain_service)


def as_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").replace("$", "").strip()
    if " " in cleaned:
        cleaned = cleaned.split(" ", 1)[0]
    try:
        return float(cleaned)
    except ValueError:
        return default


def first_value(mapping: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def pnl_percent(pnl: float, current_value: float) -> float:
    previous_value = current_value - pnl
    return pnl / abs(previous_value) * 100 if previous_value else 0.0


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    verify_tls: bool = True,
    timeout: int = 20,
) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    context = verified_ssl_context() if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise DashboardError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}") from exc
    except urllib.error.URLError as exc:
        raise DashboardError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc


def verified_ssl_context() -> ssl.SSLContext:
    """Use Python's CA bundle, falling back to the macOS system bundle when absent."""
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and Path(defaults.cafile).exists():
        return ssl.create_default_context()
    system_bundle = Path("/etc/ssl/cert.pem")
    if system_bundle.exists():
        return ssl.create_default_context(cafile=str(system_bundle))
    return ssl.create_default_context()


def request_json(
    url: str,
    *,
    verify_tls: bool = True,
    headers: dict[str, str] | None = None,
) -> Any:
    payload = request_bytes(url, verify_tls=verify_tls, headers=headers)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DashboardError(f"Invalid JSON from {urllib.parse.urlsplit(url).netloc}") from exc


def normalize_snapshot(payload: dict[str, Any], source: str) -> dict[str, Any]:
    positions = []
    for raw in payload.get("positions", []):
        if not isinstance(raw, dict):
            continue
        symbol = str(first_value(raw, ["symbol", "ticker", "description", "contract_description"], "?")).strip()
        market_value = as_float(first_value(raw, ["market_value", "marketvalue", "mktvalue", "positionvalue"]))
        daily_pnl = as_float(first_value(raw, ["daily_pnl", "dailypnl", "dpl"]))
        provided_daily_pct = first_value(raw, ["daily_change_pct", "daily_pct", "day_pct", "change_percent"])
        positions.append(
            {
                "symbol": symbol[:12],
                "quantity": as_float(first_value(raw, ["quantity", "position", "size"])),
                "market_price": as_float(first_value(raw, ["market_price", "marketprice", "mktprice", "markprice", "price"])),
                "market_value": market_value,
                "daily_pnl": daily_pnl,
                "daily_pct": as_float(provided_daily_pct) if provided_daily_pct is not None else pnl_percent(daily_pnl, market_value),
                "unrealized_pnl": as_float(first_value(raw, ["unrealized_pnl", "unrealizedpnl", "unrealizedp&l", "upl"])),
                "realized_pnl": as_float(first_value(raw, ["realized_pnl", "realizedpnl"])),
            }
        )
    currency = str(payload.get("currency") or env("DASHBOARD_CURRENCY", "USD")).upper()
    net_liquidation = as_float(first_value(payload, ["net_liquidation", "netliquidation", "net_liquidation_value", "nl"]))
    daily_pnl = as_float(first_value(payload, ["daily_pnl", "dailypnl", "dpl"]))
    provided_daily_pct = first_value(payload, ["daily_pnl_pct", "daily_pct", "day_pct"])
    return {
        "as_of": str(payload.get("as_of") or datetime.now().astimezone().isoformat(timespec="seconds")),
        "source": source,
        "currency": currency,
        "net_liquidation": net_liquidation,
        "cash": as_float(first_value(payload, ["cash", "total_cash_value", "totalcashvalue"])),
        "buying_power": as_float(first_value(payload, ["buying_power", "buyingpower"])),
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": as_float(provided_daily_pct) if provided_daily_pct is not None else pnl_percent(daily_pnl, net_liquidation),
        "unrealized_pnl": as_float(first_value(payload, ["unrealized_pnl", "unrealizedpnl", "upl"])),
        "realized_pnl": as_float(first_value(payload, ["realized_pnl", "realizedpnl"])),
        "history": [as_float(item) for item in payload.get("history", []) if item is not None],
        "nav_history": [as_float(item) for item in payload.get("nav_history", []) if item is not None],
        "return_history": [as_float(item) for item in payload.get("return_history", []) if item is not None],
        "trend_period": str(payload.get("trend_period") or "30 DAYS").upper()[:16],
        "positions": positions,
    }


def load_json_source(source: str) -> dict[str, Any]:
    if source == "-":
        try:
            payload = json.loads(sys.stdin.readline())
        except json.JSONDecodeError as exc:
            raise DashboardError("Invalid JSON received on standard input") from exc
    elif source.startswith(("https://", "http://")):
        payload = request_json(source)
    else:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DashboardError(f"JSON source not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise DashboardError(f"Invalid JSON source: {path}") from exc
    if not isinstance(payload, dict):
        raise DashboardError("JSON source must contain one object")
    source_name = str(payload.get("source") or "json").strip() or "json"
    return normalize_snapshot(payload, source_name)


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def flex_reference_code(payload: bytes) -> str:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DashboardError("IBKR Flex returned invalid XML while starting the report") from exc
    status = next((node.text for node in root.iter() if xml_local_name(node.tag) == "status"), "")
    if status and status.lower() != "success":
        error_text = next((node.text for node in root.iter() if xml_local_name(node.tag) == "errormessage"), "Flex report failed")
        raise DashboardError(str(error_text))
    code = next((node.text for node in root.iter() if xml_local_name(node.tag) == "referencecode"), None)
    if not code:
        raise DashboardError("IBKR Flex did not return a reference code")
    return code


def parse_flex_xml(payload: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DashboardError("IBKR Flex report is not valid XML; configure XML output or use a compatible CSV query") from exc
    positions: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}
    currency = env("DASHBOARD_CURRENCY", "USD")
    for node in root.iter():
        name = xml_local_name(node.tag)
        attrs = dict(node.attrib)
        if name in {"openposition", "position"}:
            positions.append(attrs)
        elif name in {"netassetvalue", "equitysummarybyreportdateinbase", "changeinnav"}:
            totals.update(attrs)
            currency = str(first_value(attrs, ["currency", "currencyprimary", "basecurrency"], currency))
    payload_dict = {
        "currency": currency,
        "net_liquidation": first_value(totals, ["endingvalue", "netliquidation", "total", "nav"]),
        "daily_pnl": first_value(totals, ["change", "dailyPnl", "pnl"]),
        "unrealized_pnl": sum(as_float(first_value(row, ["fifoPnlUnrealized", "unrealizedPnl", "unrealizedPL"])) for row in positions),
        "realized_pnl": sum(as_float(first_value(row, ["fifoPnlRealized", "realizedPnl", "realizedPL"])) for row in positions),
        "positions": positions,
    }
    return normalize_snapshot(payload_dict, "flex")


def parse_flex_csv(payload: bytes) -> dict[str, Any]:
    text = payload.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise DashboardError("IBKR Flex CSV report contained no rows")
    currency = str(first_value(rows[0], ["CurrencyPrimary", "Currency"], env("DASHBOARD_CURRENCY", "USD")))
    return normalize_snapshot(
        {
            "currency": currency,
            "positions": rows,
            "unrealized_pnl": sum(as_float(first_value(row, ["FifoPnlUnrealized", "UnrealizedPnl"])) for row in rows),
            "realized_pnl": sum(as_float(first_value(row, ["FifoPnlRealized", "RealizedPnl"])) for row in rows),
        },
        "flex",
    )


def load_flex_source() -> dict[str, Any]:
    query_id = env("IBKR_FLEX_QUERY_ID")
    token = secret("IBKR_FLEX_TOKEN", IBKR_FLEX_KEYCHAIN_SERVICE)
    if not query_id or not token:
        raise DashboardError("IBKR_FLEX_QUERY_ID and IBKR_FLEX_TOKEN are required for Flex mode")
    base = env("IBKR_FLEX_BASE_URL", "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService").rstrip("/")
    start_url = f"{base}/SendRequest?{urllib.parse.urlencode({'q': query_id, 't': token, 'v': '3'})}"
    reference_code = flex_reference_code(request_bytes(start_url))
    report_url = f"{base}/GetStatement?{urllib.parse.urlencode({'q': reference_code, 't': token, 'v': '3'})}"
    report = b""
    for attempt in range(5):
        if attempt:
            time.sleep(min(2**attempt, 8))
        report = request_bytes(report_url)
        if b"Statement generation in progress" not in report and b"1019" not in report:
            break
    stripped = report.lstrip()
    if stripped.startswith(b"<"):
        return parse_flex_xml(report)
    return parse_flex_csv(report)


def load_client_portal_source() -> dict[str, Any]:
    base = env("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api").rstrip("/")
    verify_tls = as_bool(env("IBKR_CP_VERIFY_TLS", "false"))
    accounts = request_json(f"{base}/portfolio/accounts", verify_tls=verify_tls)
    if not isinstance(accounts, list) or not accounts:
        raise DashboardError("Client Portal returned no portfolio accounts; authenticate the gateway in a browser")
    configured = env("IBKR_CP_ACCOUNT_ID")
    account_id = configured or str(first_value(accounts[0], ["accountId", "id"]))
    if not account_id:
        raise DashboardError("Client Portal account selection failed")
    encoded_account = urllib.parse.quote(account_id, safe="")
    try:
        positions = request_json(f"{base}/portfolio2/{encoded_account}/positions", verify_tls=verify_tls)
    except DashboardError:
        positions = request_json(f"{base}/portfolio/{encoded_account}/positions/0", verify_tls=verify_tls)
    pnl = request_json(f"{base}/iserver/account/pnl/partitioned", verify_tls=verify_tls)
    upnl = pnl.get("upnl", {}) if isinstance(pnl, dict) else {}
    account_pnl = next((value for key, value in upnl.items() if str(key).startswith(account_id)), {})
    if not isinstance(account_pnl, dict):
        account_pnl = {}
    ledger = request_json(f"{base}/portfolio/{encoded_account}/ledger", verify_tls=verify_tls)
    base_ledger = ledger.get("BASE", {}) if isinstance(ledger, dict) else {}
    if not base_ledger and isinstance(ledger, dict):
        base_ledger = next((value for value in ledger.values() if isinstance(value, dict)), {})
    currency = str(first_value(base_ledger, ["currency", "secondkey"], env("DASHBOARD_CURRENCY", "USD")))
    return normalize_snapshot(
        {
            "currency": currency,
            "net_liquidation": first_value(account_pnl, ["nl"], first_value(base_ledger, ["netliquidationvalue"])),
            "daily_pnl": first_value(account_pnl, ["dpl"]),
            "unrealized_pnl": first_value(account_pnl, ["upl"], first_value(base_ledger, ["unrealizedpnl"])),
            "realized_pnl": first_value(base_ledger, ["realizedpnl"]),
            "positions": positions if isinstance(positions, list) else [],
        },
        "client_portal",
    )


def fetch_snapshot() -> dict[str, Any]:
    source = env("IBKR_SOURCE", "json").lower()
    if source == "json":
        return load_json_source(env("IBKR_JSON_SOURCE") or str(SAMPLE_SNAPSHOT))
    if source == "flex":
        return load_flex_source()
    if source == "client_portal":
        return load_client_portal_source()
    raise DashboardError("IBKR_SOURCE must be json, flex, or client_portal")


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    configured = env("DASHBOARD_FONT_PATH")
    candidates = [configured] if configured else []
    candidates.extend(
        [
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def currency_prefix(currency: str) -> str:
    return {"USD": "$", "CNY": "CN¥", "HKD": "HK$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency, f"{currency} ")


def money(value: float, currency: str, compact: bool = False, signed: bool = False) -> str:
    prefix = currency_prefix(currency)
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    absolute = abs(value)
    if compact and absolute >= 1_000_000:
        body = f"{absolute / 1_000_000:.2f}M"
    elif compact and absolute >= 100_000:
        body = f"{absolute / 1_000:.1f}K"
    else:
        body = f"{absolute:,.2f}"
    return f"{sign}{prefix}{body}"


def axis_number(value: float) -> str:
    """Compact unitless labels; the chart title carries the currency."""
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if absolute >= 1_000_000:
        body = f"{absolute / 1_000_000:.1f}M"
    elif absolute >= 1_000:
        body = f"{absolute / 1_000:.1f}K"
    else:
        body = f"{absolute:.0f}"
    return f"{sign}{body}"


def quantity_label(value: float) -> str:
    if abs(value) >= 10_000:
        return f"{value / 1_000:.1f}K"
    if value.is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def draw_pnl_badge(
    draw: ImageDraw.ImageDraw,
    pnl: float,
    box: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = box
    positive = pnl > 0
    foreground = 255 if positive else 0
    background = 0 if positive else 255
    draw.rounded_rectangle(box, radius=3, fill=background, outline=0, width=1)

    middle = (top + bottom) // 2
    label = f"{pnl:+,.2f}" if pnl else "0.00"
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    icon_width = 16
    gap = 5
    group_width = icon_width + gap + text_width
    group_left = left + ((right - left + 1) - group_width) // 2

    if positive:
        trend_points = [(group_left, middle + 5), (group_left + 7, middle + 2), (group_left + 16, middle - 4)]
    elif pnl < 0:
        trend_points = [(group_left, middle - 5), (group_left + 7, middle - 2), (group_left + 16, middle + 4)]
    else:
        trend_points = [(group_left, middle), (group_left + 7, middle), (group_left + 16, middle)]
    draw.line(trend_points, fill=foreground, width=1)
    draw.text(
        (group_left + icon_width + gap, top + (bottom - top - text_height) // 2 - text_box[1]),
        label,
        font=font,
        fill=foreground,
    )


def display_timestamp(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    timezone = ZoneInfo(timezone_name)
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)
    except ValueError:
        timestamp = datetime.now(timezone)
    return f"UTC+8 {timestamp:%m/%d %H:%M}"


def append_history(snapshot: dict[str, Any], path: Path, limit: int = 30) -> list[float]:
    """Keep one NAV sample per local calendar day for a true 30-day window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                history = [item for item in raw if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            history = []

    timezone = ZoneInfo(env("DASHBOARD_TIMEZONE", "Asia/Shanghai"))
    try:
        timestamp = datetime.fromisoformat(snapshot["as_of"].replace("Z", "+00:00")).astimezone(timezone)
    except ValueError:
        timestamp = datetime.now(timezone)
    sample_day = timestamp.date().isoformat()
    history = [
        item
        for item in history
        if str(item.get("date") or item.get("at", ""))[:10] != sample_day
    ]
    history.append({"date": sample_day, "net_liquidation": snapshot["net_liquidation"]})
    history = history[-limit:]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(history, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return [as_float(item.get("net_liquidation")) for item in history]


def scaled_points(values: list[float], box: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    left, top, right, bottom = box
    if len(values) < 2:
        return []
    low, high = min(values), max(values)
    span = high - low or 1.0
    points: list[tuple[int, int]] = []
    for index, value in enumerate(values):
        x = left + index * (right - left) / (len(values) - 1)
        y = bottom - (value - low) * (bottom - top) / span
        points.append((round(x), round(y)))
    return points


def draw_nav_trend(
    draw: ImageDraw.ImageDraw,
    nav_values: list[float],
    box: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = box
    draw.rectangle(box, outline=0, width=1)
    plot_box = (left + 5, top + 8, right - 5, bottom - 8)
    plot_left, plot_top, plot_right, plot_bottom = plot_box

    if nav_values:
        nav_low, nav_high = min(nav_values), max(nav_values)
        span = nav_high - nav_low
        for index in range(5):
            ratio = index / 4
            y = round(plot_top + ratio * (plot_bottom - plot_top))
            value = nav_high - ratio * span
            label = axis_number(value)
            label_width = draw.textbbox((0, 0), label, font=font)[2]
            draw.text((left - 4 - label_width, y - 5), label, font=font, fill=0)
            for x in range(plot_left, plot_right + 1, 4):
                draw.point((x, y), fill=120)
        nav_points = scaled_points(nav_values, plot_box)
        if nav_points:
            draw.line(nav_points, fill=0, width=2)
            x, y = nav_points[-1]
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=0)


def render(snapshot: dict[str, Any], output: Path, history: list[float] | None = None) -> Path:
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    regular_10 = load_font(10)
    regular_12 = load_font(12)
    bold_12 = load_font(12, bold=True)
    bold_16 = load_font(16, bold=True)
    bold_25 = load_font(25, bold=True)
    currency = snapshot["currency"]

    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0, width=1)
    draw.text((12, 8), "IBKR PORTFOLIO", font=bold_16, fill=0)
    time_text = display_timestamp(snapshot["as_of"], env("DASHBOARD_TIMEZONE", "Asia/Shanghai"))
    time_width = draw.textbbox((0, 0), time_text, font=regular_10)[2]
    draw.text((WIDTH - 12 - time_width, 11), time_text, font=regular_10, fill=0)
    draw.line((10, 32, WIDTH - 10, 32), fill=0, width=1)

    draw.text((12, 41), "NET LIQUIDATION", font=regular_10, fill=0)
    draw.text((12, 54), money(snapshot["net_liquidation"], currency, compact=True), font=bold_25, fill=0)
    day_text = money(snapshot["daily_pnl"], currency, compact=True, signed=True)
    draw.text((12, 86), f"DAY  {day_text}", font=bold_12, fill=0)
    draw.text((12, 104), f"UNREAL  {money(snapshot['unrealized_pnl'], currency, compact=True, signed=True)}", font=regular_10, fill=0)
    draw.text((12, 120), f"DAY %  {snapshot['daily_pnl_pct']:+.2f}%", font=regular_10, fill=0)

    nav_trend = snapshot.get("nav_history") or history or snapshot.get("history", [])
    trend_title = f"{snapshot.get('trend_period', '30 DAYS')} NAV ({currency})"
    trend_title_width = draw.textbbox((0, 0), trend_title, font=bold_12)[2]
    draw.text((320 - trend_title_width // 2, 39), trend_title, font=bold_12, fill=0)
    draw_nav_trend(
        draw,
        [as_float(value) for value in nav_trend],
        (250, 54, 390, 142),
        regular_10,
    )

    draw.rectangle((10, 150, 390, 272), outline=0, width=1)
    draw.rectangle((10, 150, 390, 172), fill=0)
    draw.text((18, 155), "STOCK", font=bold_12, fill=255)
    draw.text((92, 155), "POSITION", font=bold_12, fill=255)
    draw.text((188, 155), "PRICE", font=bold_12, fill=255)
    draw.text((300, 155), "DAY P&L", font=bold_12, fill=255)

    max_positions = max(1, int(env("DASHBOARD_MAX_POSITIONS", "4") or "4"))
    positions = sorted(snapshot["positions"], key=lambda item: abs(item["market_value"]), reverse=True)[:max_positions]
    if not positions:
        draw.text((18, 182), "No open positions in this snapshot", font=regular_12, fill=0)
    for index, position in enumerate(positions):
        row_top = 176 + index * 23
        if row_top > 250:
            break
        text_y = row_top + 2
        draw.text((18, text_y), position["symbol"], font=bold_12, fill=0)
        draw.text((110, text_y), quantity_label(position["quantity"]), font=regular_12, fill=0)
        draw.text((188, text_y), f"{position['market_price']:,.2f}", font=regular_12, fill=0)
        draw_pnl_badge(draw, position["daily_pnl"], (286, row_top, 382, row_top + 19), regular_10)
        if index < len(positions) - 1:
            draw.line((18, row_top + 21, 382, row_top + 21), fill=190, width=1)

    footer = (
        f"CASH {money(snapshot['cash'], currency, compact=True)}"
        f" · BUYING POWER {money(snapshot['buying_power'], currency, compact=True)}"
    )
    draw.text((12, 280), footer, font=regular_10, fill=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("1", dither=Image.Dither.NONE).save(output, format="PNG", optimize=True)
    return output


def multipart_image(image_path: Path, page_id: str) -> tuple[bytes, str]:
    boundary = f"----ibkr-zectrix-{uuid.uuid4().hex}"
    buffer = io.BytesIO()
    for name, value in (("dither", "false"), ("pageId", page_id)):
        buffer.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    buffer.write(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"images\"; filename=\"dashboard.png\"\r\nContent-Type: image/png\r\n\r\n".encode()
    )
    buffer.write(image_path.read_bytes())
    buffer.write(f"\r\n--{boundary}--\r\n".encode())
    return buffer.getvalue(), boundary


def masked_identifier(value: str) -> str:
    compact = value.replace(":", "").replace("-", "")
    if len(compact) <= 4:
        return "*" * len(compact)
    return f"{'*' * (len(compact) - 4)}{compact[-4:]}"


def get_zectrix_devices(api_key: str) -> list[dict[str, Any]]:
    base = env("ZECTRIX_API_BASE_URL", "https://cloud.zectrix.com").rstrip("/")
    response = request_json(f"{base}/open/v1/devices", headers={"X-API-Key": api_key})
    if not isinstance(response, dict):
        raise DashboardError("ZECTRIX devices response was not an object")
    if response.get("code") not in (None, 0):
        raise DashboardError(f"ZECTRIX rejected the API key: code {response.get('code')}")
    devices = response.get("data", [])
    if not isinstance(devices, list):
        raise DashboardError("ZECTRIX devices response did not contain a device list")
    return [device for device in devices if isinstance(device, dict)]


def select_zectrix_device(api_key: str) -> str:
    configured = env("ZECTRIX_DEVICE_ID")
    if configured:
        return configured
    devices = get_zectrix_devices(api_key)
    if not devices:
        raise DashboardError("No ZECTRIX devices are bound to this account")
    if len(devices) != 1:
        raise DashboardError("Multiple ZECTRIX devices are bound; set ZECTRIX_DEVICE_ID in the runtime environment")
    device_id = str(first_value(devices[0], ["deviceId", "id"], "")).strip()
    if not device_id:
        raise DashboardError("The bound ZECTRIX device has no device ID")
    return device_id


def push_zectrix(image_path: Path) -> None:
    api_key = secret("ZECTRIX_API_KEY", ZECTRIX_KEYCHAIN_SERVICE)
    if not api_key:
        raise DashboardError("ZECTRIX_API_KEY or its macOS Keychain item is required to push")
    device_id = select_zectrix_device(api_key)
    base = env("ZECTRIX_API_BASE_URL", "https://cloud.zectrix.com").rstrip("/")
    page_id = env("ZECTRIX_PAGE_ID", "1")
    body, boundary = multipart_image(image_path, page_id)
    url = f"{base}/open/v1/devices/{urllib.parse.quote(device_id, safe='')}/display/image"
    response = request_bytes(
        url,
        method="POST",
        headers={"X-API-Key": api_key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body,
    )
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise DashboardError("ZECTRIX returned an invalid response") from exc
    if isinstance(parsed, dict) and parsed.get("code") not in (None, 0):
        raise DashboardError(f"ZECTRIX rejected the image: code {parsed.get('code')}")


def command_preview(args: argparse.Namespace) -> None:
    snapshot = load_json_source(args.input)
    path = render(snapshot, Path(args.output).expanduser(), snapshot.get("history"))
    print(f"Preview written: {path}")


def command_devices(_args: argparse.Namespace) -> None:
    api_key = secret("ZECTRIX_API_KEY", ZECTRIX_KEYCHAIN_SERVICE)
    if not api_key:
        raise DashboardError(
            "ZECTRIX API key not found; store it in macOS Keychain service "
            f"{ZECTRIX_KEYCHAIN_SERVICE} or set ZECTRIX_API_KEY"
        )
    devices = get_zectrix_devices(api_key)
    print(f"ZECTRIX devices: {len(devices)}")
    for device in devices:
        device_id = str(first_value(device, ["deviceId", "id"], ""))
        alias = str(first_value(device, ["alias", "name"], "Unnamed"))
        board = str(first_value(device, ["board", "model"], "unknown"))
        print(f"- alias={alias!r} board={board!r} id={masked_identifier(device_id)}")


def command_init(args: argparse.Namespace) -> None:
    output = Path(args.output).expanduser()
    if output.exists() and not args.force:
        raise DashboardError(f"Configuration already exists: {output}; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(ENV_TEMPLATE.format(sample=SAMPLE_SNAPSHOT), encoding="utf-8")
    output.chmod(0o600)
    print(f"Configuration template written: {output}")


def command_doctor(_args: argparse.Namespace) -> None:
    source = env("IBKR_SOURCE", "json").lower()
    issues: list[str] = []
    if source == "json":
        location = env("IBKR_JSON_SOURCE") or str(SAMPLE_SNAPSHOT)
        if not location.startswith(("https://", "http://")) and not Path(location).expanduser().exists():
            issues.append(f"JSON source does not exist: {location}")
    elif source == "flex":
        if not env("IBKR_FLEX_QUERY_ID"):
            issues.append("IBKR_FLEX_QUERY_ID is missing")
        if not secret("IBKR_FLEX_TOKEN", IBKR_FLEX_KEYCHAIN_SERVICE):
            issues.append("IBKR_FLEX_TOKEN is missing")
    elif source == "client_portal":
        if not env("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api"):
            issues.append("IBKR_CP_BASE_URL is missing")
    else:
        issues.append("IBKR_SOURCE must be json, flex, or client_portal")

    print(f"IBKR source: {source}")
    print(f"Sample snapshot: {SAMPLE_SNAPSHOT}")
    print(f"ZECTRIX credential: {'present' if secret('ZECTRIX_API_KEY', ZECTRIX_KEYCHAIN_SERVICE) else 'missing (preview only)'}")
    if issues:
        raise DashboardError("; ".join(issues))
    print("Configuration checks passed")


def command_run(args: argparse.Namespace) -> None:
    snapshot = fetch_snapshot()
    state_path = Path(env("DASHBOARD_STATE_PATH", "state/history.json")).expanduser()
    if snapshot.get("nav_history"):
        history = snapshot["nav_history"][-30:]
    else:
        history = append_history(snapshot, state_path)
    if len(history) < 2 and snapshot.get("history"):
        history = snapshot["history"]
    output = Path(args.output or env("DASHBOARD_OUTPUT_PATH", "output/ibkr-dashboard.png")).expanduser()
    render(snapshot, output, history)
    if not args.no_push:
        push_zectrix(output)
        print(f"Dashboard pushed: {output}")
    else:
        print(f"Dashboard rendered without push: {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="KEY=VALUE file loaded before the command")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="Render a supplied JSON snapshot")
    preview.add_argument("--input", default=str(SAMPLE_SNAPSHOT))
    preview.add_argument("--output", default="output/preview.png")
    preview.set_defaults(handler=command_preview)

    run = subparsers.add_parser("run", help="Fetch, render, and optionally push")
    run.add_argument("--output")
    run.add_argument("--no-push", action="store_true")
    run.set_defaults(handler=command_run)

    devices = subparsers.add_parser("devices", help="Validate ZECTRIX auth and list masked devices")
    devices.set_defaults(handler=command_devices)

    push = subparsers.add_parser("push", help="Push an existing PNG to ZECTRIX")
    push.add_argument("image")
    push.set_defaults(handler=lambda args: push_zectrix(Path(args.image).expanduser()))

    initialize = subparsers.add_parser("init", help="Write a secret-safe environment template")
    initialize.add_argument("--output", default=".env")
    initialize.add_argument("--force", action="store_true")
    initialize.set_defaults(handler=command_init)

    doctor = subparsers.add_parser("doctor", help="Check local configuration without placing orders")
    doctor.set_defaults(handler=command_doctor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_env_file(args.env_file)
    try:
        args.handler(args)
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
