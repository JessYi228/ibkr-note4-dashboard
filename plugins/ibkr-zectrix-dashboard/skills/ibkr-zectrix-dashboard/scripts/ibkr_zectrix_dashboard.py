#!/usr/bin/env python3
"""Read-only IBKR portfolio snapshot renderer and ZECTRIX NOTE4 delivery client."""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
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
USER_AGENT = "ibkr-zectrix-dashboard/0.2.1"
ZECTRIX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/zectrix-api-key"
IBKR_FLEX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/ibkr-flex-token"
PREFERENCE_FIELD_BY_ENV = {
    "IBKR_SOURCE": "data_source",
    "IBKR_ZECTRIX_SECRET_BACKEND": "secret_backend",
    "DASHBOARD_TIMEZONE": "timezone",
    "DASHBOARD_CURRENCY": "currency",
    "ZECTRIX_PAGE_ID": "page_id",
    "DASHBOARD_MAX_POSITIONS": "max_positions",
}
ALLOWED_PREFERENCE_FIELDS = frozenset(PREFERENCE_FIELD_BY_ENV.values())
FORBIDDEN_PREFERENCE_FIELDS = frozenset(
    {
        "IBKR_FLEX_TOKEN",
        "IBKR_FLEX_QUERY_ID",
        "IBKR_CP_ACCOUNT_ID",
        "ZECTRIX_API_KEY",
        "ZECTRIX_DEVICE_ID",
        "account_id",
        "device_id",
        "api_key",
        "token",
    }
)
PIXEL_DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


class DashboardError(RuntimeError):
    """Actionable dashboard error with secret-safe text."""


def ensure_private_parent(target: Path) -> None:
    """Create a target directory privately without changing an existing parent."""
    parent = target.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and not parent_existed:
        os.chmod(parent, 0o700)


def write_private_text(target: Path, text: str) -> None:
    """Atomically replace a text file with owner-only permissions on POSIX."""
    ensure_private_parent(target)
    temporary = target.with_suffix(target.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name == "posix":
        os.chmod(temporary, 0o600)
    temporary.replace(target)
    if os.name == "posix":
        os.chmod(target, 0o600)


def save_private_png(image: Image.Image, output: Path) -> None:
    """Atomically save a PNG with owner-only permissions on POSIX."""
    ensure_private_parent(output)
    temporary = output.with_suffix(output.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)
    image.save(temporary, format="PNG", optimize=True)
    if os.name == "posix":
        os.chmod(temporary, 0o600)
    temporary.replace(output)
    if os.name == "posix":
        os.chmod(output, 0o600)


def preferences_path() -> Path:
    override = os.environ.get("IBKR_ZECTRIX_PREFERENCES_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "")).expanduser() if os.environ.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "ibkr-zectrix-dashboard" / "preferences.json"


def load_preferences(path: Path | None = None) -> dict[str, Any]:
    target = path or preferences_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardError(f"Preferences could not be read: {target}") from exc
    if not isinstance(raw, dict):
        raise DashboardError("Preferences must be a JSON object")
    if os.name == "posix" and target.stat().st_mode & 0o077:
        raise DashboardError(f"Preferences permissions are too broad; run chmod 600 {target}")
    forbidden = sorted(set(raw) & FORBIDDEN_PREFERENCE_FIELDS)
    if forbidden:
        raise DashboardError(
            "Preferences must never contain credentials or identifiers; remove: " + ", ".join(forbidden)
        )
    unknown = sorted(set(raw) - ALLOWED_PREFERENCE_FIELDS - {"schema_version"})
    if unknown:
        raise DashboardError("Unknown preferences fields: " + ", ".join(unknown))
    if raw.get("schema_version", 1) != 1:
        raise DashboardError("Unsupported preferences schema_version")
    return {key: raw[key] for key in ALLOWED_PREFERENCE_FIELDS if key in raw}


def save_preferences(values: dict[str, Any], path: Path | None = None) -> Path:
    target = path or preferences_path()
    forbidden = sorted(set(values) & FORBIDDEN_PREFERENCE_FIELDS)
    if forbidden:
        raise DashboardError("Refusing to store secret preferences: " + ", ".join(forbidden))
    unknown = sorted(set(values) - ALLOWED_PREFERENCE_FIELDS)
    if unknown:
        raise DashboardError("Refusing to store unknown preferences: " + ", ".join(unknown))
    payload = {"schema_version": 1, **values}
    write_private_text(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return target


def env(name: str, default: str = "") -> str:
    configured = os.environ.get(name)
    if configured is not None and configured.strip():
        return configured.strip()
    preference_field = PREFERENCE_FIELD_BY_ENV.get(name)
    if preference_field:
        value = load_preferences().get(preference_field)
        if value is not None:
            return str(value).strip()
    return default.strip()


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
    backend = env("IBKR_ZECTRIX_SECRET_BACKEND", "auto").lower()
    if backend not in {"auto", "environment", "keychain"}:
        raise DashboardError("secret_backend must be auto, environment, or keychain")
    environment_value = os.environ.get(env_name, "").strip()
    if backend == "environment":
        return environment_value
    if backend == "keychain":
        return keychain_secret(keychain_service)
    return environment_value or keychain_secret(keychain_service)


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
        return load_json_source(env("IBKR_JSON_SOURCE", "assets/sample_snapshot.json"))
    if source == "flex":
        return load_flex_source()
    if source == "client_portal":
        return load_client_portal_source()
    if source == "codex_ibkr":
        raise DashboardError("codex_ibkr uses the relay command with sanitized JSON on standard input")
    raise DashboardError("IBKR_SOURCE must be codex_ibkr, json, flex, or client_portal")


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


def draw_eink_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: str,
    *,
    font: ImageFont.ImageFont,
    fill: int,
) -> None:
    """Draw unambiguous 5x7 digits at small e-paper sizes."""
    text = str(value)
    font_size = int(getattr(font, "size", 99))
    if font_size > 12 or not any(character in PIXEL_DIGITS for character in text):
        draw.text(xy, text, font=font, fill=fill)
        return

    x, y = xy
    index = 0
    while index < len(text):
        character = text[index]
        if character not in PIXEL_DIGITS:
            end = index + 1
            while end < len(text) and text[end] not in PIXEL_DIGITS:
                end += 1
            segment = text[index:end]
            draw.text((x, y), segment, font=font, fill=fill)
            x += float(draw.textlength(segment, font=font))
            index = end
            continue

        advance = float(draw.textlength(character, font=font))
        bounds = draw.textbbox((x, y), character, font=font)
        glyph_x = round(x + (advance - 5) / 2)
        glyph_y = round(bounds[1] + ((bounds[3] - bounds[1]) - 7) / 2)
        for row, bits in enumerate(PIXEL_DIGITS[character]):
            for column, bit in enumerate(bits):
                if bit == "1":
                    draw.point((glyph_x + column, glyph_y + row), fill=fill)
        x += advance
        index += 1


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
    draw_eink_text(
        draw,
        (group_left + icon_width + gap, top + (bottom - top - text_height) // 2 - text_box[1]),
        label,
        font=font,
        fill=foreground,
    )


def display_timestamp(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    try:
        timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise DashboardError(f"Unknown timezone: {timezone_name}") from exc
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)
    except ValueError:
        timestamp = datetime.now(timezone)
    offset = timestamp.utcoffset()
    offset_minutes = round((offset.total_seconds() if offset else 0) / 60)
    if offset_minutes == 0:
        offset_label = "UTC"
    else:
        sign = "+" if offset_minutes > 0 else "-"
        hours, minutes = divmod(abs(offset_minutes), 60)
        offset_label = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return f"{offset_label} {timestamp:%m/%d %H:%M}"


def append_history(snapshot: dict[str, Any], path: Path, limit: int = 96) -> list[float]:
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                history = [item for item in raw if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            history = []
    history.append({"at": snapshot["as_of"], "net_liquidation": snapshot["net_liquidation"]})
    history = history[-limit:]
    write_private_text(path, json.dumps(history, separators=(",", ":")))
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
            draw_eink_text(draw, (left - 4 - label_width, y - 5), label, font=font, fill=0)
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
    draw_eink_text(draw, (WIDTH - 12 - time_width, 11), time_text, font=regular_10, fill=0)
    draw.line((10, 32, WIDTH - 10, 32), fill=0, width=1)

    draw.text((12, 41), "NET LIQUIDATION", font=regular_10, fill=0)
    draw.text((12, 54), money(snapshot["net_liquidation"], currency, compact=True), font=bold_25, fill=0)
    day_text = money(snapshot["daily_pnl"], currency, compact=True, signed=True)
    draw_eink_text(draw, (12, 86), f"DAY  {day_text}", font=bold_12, fill=0)
    draw_eink_text(
        draw,
        (12, 104),
        f"UNREAL  {money(snapshot['unrealized_pnl'], currency, compact=True, signed=True)}",
        font=regular_10,
        fill=0,
    )
    draw_eink_text(draw, (12, 120), f"DAY %  {snapshot['daily_pnl_pct']:+.2f}%", font=regular_10, fill=0)

    nav_trend = snapshot.get("nav_history") or history or snapshot.get("history", [])
    trend_title = f"{snapshot.get('trend_period', '30 DAYS')} NAV ({currency})"
    trend_title_width = draw.textbbox((0, 0), trend_title, font=bold_12)[2]
    draw_eink_text(draw, (320 - trend_title_width // 2, 39), trend_title, font=bold_12, fill=0)
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
        draw_eink_text(draw, (110, text_y), quantity_label(position["quantity"]), font=regular_12, fill=0)
        draw_eink_text(draw, (188, text_y), f"{position['market_price']:,.2f}", font=regular_12, fill=0)
        draw_pnl_badge(draw, position["daily_pnl"], (286, row_top, 382, row_top + 19), regular_10)
        if index < len(positions) - 1:
            draw.line((18, row_top + 21, 382, row_top + 21), fill=190, width=1)

    footer = (
        f"CASH {money(snapshot['cash'], currency, compact=True)}"
        f" · BUYING POWER {money(snapshot['buying_power'], currency, compact=True)}"
    )
    draw_eink_text(draw, (12, 280), footer, font=regular_10, fill=0)
    save_private_png(image.convert("1", dither=Image.Dither.NONE), output)
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


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Hash display data while ignoring timestamps and source identifiers."""
    positions = sorted(
        (
            {
                "symbol": position["symbol"],
                "quantity": position["quantity"],
                "market_price": position["market_price"],
                "market_value": position["market_value"],
                "daily_pnl": position["daily_pnl"],
                "unrealized_pnl": position["unrealized_pnl"],
            }
            for position in snapshot["positions"]
        ),
        key=lambda item: (item["symbol"], item["quantity"]),
    )
    display_data = {
        "currency": snapshot["currency"],
        "net_liquidation": snapshot["net_liquidation"],
        "cash": snapshot["cash"],
        "buying_power": snapshot["buying_power"],
        "daily_pnl": snapshot["daily_pnl"],
        "unrealized_pnl": snapshot["unrealized_pnl"],
        "realized_pnl": snapshot["realized_pnl"],
        "nav_history": snapshot.get("nav_history", []),
        "history": snapshot.get("history", []),
        "trend_period": snapshot.get("trend_period", ""),
        "positions": positions,
    }
    encoded = json.dumps(display_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_last_push_fingerprint(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    fingerprint = payload.get("sha256")
    return fingerprint if isinstance(fingerprint, str) else ""


def write_last_push_fingerprint(path: Path, fingerprint: str) -> None:
    payload = {
        "sha256": fingerprint,
        "pushed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_private_text(path, json.dumps(payload, separators=(",", ":")))


def display_path(path: Path) -> str:
    try:
        return f"~/{path.resolve().relative_to(Path.home().resolve())}"
    except (OSError, ValueError):
        return str(path)


def required_runtime_values(source: str) -> list[str]:
    required = ["ZECTRIX_API_KEY"]
    if source == "flex":
        required.extend(["IBKR_FLEX_QUERY_ID", "IBKR_FLEX_TOKEN"])
    elif source == "client_portal":
        required.append("authenticated Client Portal Gateway session")
    elif source == "codex_ibkr":
        required.append("connected read-only IBKR plugin session")
    elif source == "json":
        required.append("IBKR_JSON_SOURCE")
    return required


def command_configure(args: argparse.Namespace) -> None:
    if args.max_positions is not None and args.max_positions < 1:
        raise DashboardError("max_positions must be at least 1")
    if args.currency and (len(args.currency) != 3 or not args.currency.isalpha()):
        raise DashboardError("currency must be a three-letter code such as USD")
    if args.timezone:
        try:
            ZoneInfo(args.timezone)
        except (KeyError, ValueError) as exc:
            raise DashboardError(f"Unknown timezone: {args.timezone}") from exc
    preferences = load_preferences()
    updates = {
        "data_source": args.source,
        "secret_backend": args.secret_backend,
        "timezone": args.timezone,
        "currency": args.currency.upper() if args.currency else None,
        "page_id": args.page_id,
        "max_positions": args.max_positions,
    }
    preferences.update({key: value for key, value in updates.items() if value is not None})
    if not preferences:
        raise DashboardError("Choose at least one non-secret preference to save")
    target = save_preferences(preferences)
    source = str(preferences.get("data_source", "json"))
    backend = str(preferences.get("secret_backend", "auto"))
    print(f"Non-secret preferences saved: {display_path(target)}")
    print("Credentials and account/device identifiers were not requested or stored.")
    print(f"Preferred data source: {source}; secret backend: {backend}")
    if backend == "keychain":
        if sys.platform == "darwin":
            print("Store secrets from your own terminal; each command prompts without echoing the value:")
            print(
                'security add-generic-password -U -a "$USER" '
                f'-s "{ZECTRIX_KEYCHAIN_SERVICE}" -w'
            )
            if source == "flex":
                print(
                    'security add-generic-password -U -a "$USER" '
                    f'-s "{IBKR_FLEX_KEYCHAIN_SERVICE}" -w'
                )
                print("Inject IBKR_FLEX_QUERY_ID through the runtime environment; do not store it here.")
        else:
            print("macOS Keychain is unavailable on this platform; choose environment for the next configure run.")
    else:
        print("Inject these values as runtime environment variables from a local protected file or cloud secret manager:")
        for name in required_runtime_values(source):
            print(f"- {name}")
        print("Do not place their values in preferences.json, source control, chat, screenshots, or logs.")


def command_settings(args: argparse.Namespace) -> None:
    preferences = load_preferences()
    source = str(preferences.get("data_source", "json"))
    backend = str(preferences.get("secret_backend", "auto"))
    zectrix_present = bool(secret("ZECTRIX_API_KEY", ZECTRIX_KEYCHAIN_SERVICE))
    flex_present = bool(secret("IBKR_FLEX_TOKEN", IBKR_FLEX_KEYCHAIN_SERVICE)) if source == "flex" else None
    result = {
        "preferences_path": display_path(preferences_path()),
        "preferences": preferences,
        "required_runtime_values": required_runtime_values(source),
        "credential_status": {
            "zectrix_api_key": "present" if zectrix_present else "absent",
            "ibkr_flex_token": None if flex_present is None else ("present" if flex_present else "absent"),
        },
        "secrets_stored_in_preferences": False,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Preferences: {result['preferences_path']}")
    print(json.dumps(preferences, indent=2, sort_keys=True))
    print(f"ZECTRIX API key: {result['credential_status']['zectrix_api_key']}")
    if flex_present is not None:
        print(f"IBKR Flex token: {result['credential_status']['ibkr_flex_token']}")
    print("Secret values are never displayed.")


def command_relay(args: argparse.Namespace) -> None:
    """Render a sanitized Codex-IBKR snapshot and optionally push it atomically."""
    snapshot = load_json_source(args.input)
    if snapshot["source"] != "codex_ibkr":
        raise DashboardError("Relay input must declare source=codex_ibkr; sample fallback is disabled")
    output = Path(args.output).expanduser()
    render(snapshot, output, snapshot.get("nav_history") or snapshot.get("history"))
    fingerprint = snapshot_fingerprint(snapshot)
    if not args.push:
        print(f"Relay preview written: {output}")
        return

    state_path = Path(args.dedupe_state).expanduser()
    if not args.force and read_last_push_fingerprint(state_path) == fingerprint:
        print("Relay unchanged: push skipped")
        return
    push_zectrix(output)
    write_last_push_fingerprint(state_path, fingerprint)
    print(f"Relay pushed: {output}")


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


def command_run(args: argparse.Namespace) -> None:
    snapshot = fetch_snapshot()
    state_path = Path(env("DASHBOARD_STATE_PATH", "state/history.json")).expanduser()
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="Remember non-secret choices and show secret setup steps")
    configure.add_argument("--source", choices=("codex_ibkr", "flex", "client_portal", "json"))
    configure.add_argument("--secret-backend", choices=("auto", "keychain", "environment"))
    configure.add_argument("--timezone")
    configure.add_argument("--currency")
    configure.add_argument("--page-id")
    configure.add_argument("--max-positions", type=int)
    configure.set_defaults(handler=command_configure)

    settings = subparsers.add_parser("settings", help="Show saved choices and redacted credential readiness")
    settings.add_argument("--json", action="store_true")
    settings.set_defaults(handler=command_settings)

    preview = subparsers.add_parser("preview", help="Render a supplied JSON snapshot")
    preview.add_argument("--input", default="assets/sample_snapshot.json")
    preview.add_argument("--output", default="output/preview.png")
    preview.set_defaults(handler=command_preview)

    run = subparsers.add_parser("run", help="Fetch, render, and optionally push")
    run.add_argument("--output")
    run.add_argument("--no-push", action="store_true")
    run.set_defaults(handler=command_run)

    relay = subparsers.add_parser("relay", help="Render a sanitized Codex-IBKR snapshot without sample fallback")
    relay.add_argument("--input", default="-", help="JSON input; use - for standard input")
    relay.add_argument("--output", default="output/ibkr-dashboard.png")
    relay.add_argument("--push", action="store_true")
    relay.add_argument("--dedupe-state", default="state/last-push.json")
    relay.add_argument("--force", action="store_true", help="Push even when the sanitized snapshot is unchanged")
    relay.set_defaults(handler=command_relay)

    devices = subparsers.add_parser("devices", help="Validate ZECTRIX auth and list masked devices")
    devices.set_defaults(handler=command_devices)

    push = subparsers.add_parser("push", help="Push an existing PNG to ZECTRIX")
    push.add_argument("image")
    push.set_defaults(handler=lambda args: push_zectrix(Path(args.image).expanduser()))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
