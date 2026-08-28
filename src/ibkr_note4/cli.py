#!/usr/bin/env python3
"""Read-only IBKR portfolio snapshot renderer and ZECTRIX NOTE4 delivery client."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont


WIDTH = 400
HEIGHT = 300
USER_AGENT = "ibkr-note4-dashboard/0.2"
ZECTRIX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/zectrix-api-key"
IBKR_FLEX_KEYCHAIN_SERVICE = "ibkr-zectrix-dashboard/ibkr-flex-token"
PACKAGE_ROOT = Path(__file__).resolve().parent
SAMPLE_SNAPSHOT = PACKAGE_ROOT / "assets" / "sample_snapshot.json"
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

ENV_TEMPLATE = """# Choose: json, flex, or client_portal
IBKR_SOURCE=flex
IBKR_JSON_SOURCE=

# IBKR Flex Web Service
IBKR_FLEX_QUERY_ID=
IBKR_FLEX_DAILY_QUERY_ID=
IBKR_FLEX_TOKEN=
IBKR_FLEX_BASE_URL=https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService
IBKR_FLEX_PERIOD_DAYS=30
IBKR_FLEX_POLL_ATTEMPTS=8

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
DASHBOARD_DEDUPE_STATE_PATH=state/last-push.json

# Reliability (optional)
ZECTRIX_PUSH_ATTEMPTS=3
HEALTHCHECK_URL=
"""


class DashboardError(RuntimeError):
    """Actionable dashboard error with secret-safe text."""


class HTTPStatusError(DashboardError):
    """HTTP error retaining only the status and destination host."""

    def __init__(self, status: int, host: str):
        super().__init__(f"HTTP {status} from {host}")
        self.status = status


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def dashboard_timezone(name: str | None = None) -> ZoneInfo:
    timezone_name = name or env("DASHBOARD_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise DashboardError(f"Unknown DASHBOARD_TIMEZONE: {timezone_name}") from exc


def require_https_url(url: str, label: str, *, allow_local_http: bool = False) -> None:
    parsed = urllib.parse.urlsplit(url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https" and parsed.hostname:
        return
    if allow_local_http and parsed.scheme == "http" and parsed.hostname in local_hosts:
        return
    raise DashboardError(f"{label} must use HTTPS")


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


def optional_float(value: Any) -> float | None:
    """Parse a finite number while preserving unavailable data as None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        if " " in cleaned:
            cleaned = cleaned.split(" ", 1)[0]
        try:
            number = float(cleaned)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def first_value(mapping: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def pnl_percent(pnl: float | None, current_value: float | None) -> float | None:
    if pnl is None or current_value is None:
        return None
    previous_value = current_value - pnl
    return pnl / abs(previous_value) * 100 if previous_value else None


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    verify_tls: bool = True,
    timeout: int = 20,
    attempts: int = 1,
) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    context = verified_ssl_context() if verify_tls else ssl._create_unverified_context()
    host = urllib.parse.urlsplit(url).netloc
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise HTTPStatusError(exc.code, host) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt == attempts - 1:
                reason = getattr(exc, "reason", exc)
                raise DashboardError(f"Could not reach {host}: {reason}") from exc
        time.sleep(min(2**attempt, 8))
    raise DashboardError(f"Could not reach {host}")


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
        market_value = optional_float(first_value(raw, ["market_value", "marketvalue", "mktvalue", "positionvalue"]))
        daily_pnl = optional_float(first_value(raw, ["daily_pnl", "dailypnl", "dpl"]))
        provided_daily_pct = first_value(raw, ["daily_change_pct", "daily_pct", "day_pct", "change_percent"])
        positions.append(
            {
                "symbol": symbol[:12],
                "quantity": optional_float(first_value(raw, ["quantity", "position", "size"])) or 0.0,
                "market_price": optional_float(first_value(raw, ["market_price", "marketprice", "mktprice", "markprice", "price"])),
                "market_value": market_value,
                "daily_pnl": daily_pnl,
                "daily_pct": optional_float(provided_daily_pct) if provided_daily_pct is not None else pnl_percent(daily_pnl, market_value),
                "unrealized_pnl": optional_float(first_value(raw, ["unrealized_pnl", "unrealizedpnl", "unrealizedp&l", "upl", "fifopnlunrealized"])),
                "realized_pnl": optional_float(first_value(raw, ["realized_pnl", "realizedpnl", "fifopnlrealized"])),
            }
        )
    currency = str(payload.get("currency") or env("DASHBOARD_CURRENCY", "USD")).upper()
    net_liquidation = optional_float(first_value(payload, ["net_liquidation", "netliquidation", "net_liquidation_value", "nl"]))
    daily_pnl = optional_float(first_value(payload, ["daily_pnl", "dailypnl", "dpl"]))
    provided_daily_pct = first_value(payload, ["daily_pnl_pct", "daily_pct", "day_pct"])
    return {
        "as_of": str(payload.get("as_of") or datetime.now().astimezone().isoformat(timespec="seconds")),
        "source": source,
        "currency": currency,
        "net_liquidation": net_liquidation,
        "cash": optional_float(first_value(payload, ["cash", "total_cash_value", "totalcashvalue", "endingcash", "cashbalance"])),
        "buying_power": optional_float(first_value(payload, ["buying_power", "buyingpower"])),
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": optional_float(provided_daily_pct) if provided_daily_pct is not None else pnl_percent(daily_pnl, net_liquidation),
        "unrealized_pnl": optional_float(first_value(payload, ["unrealized_pnl", "unrealizedpnl", "upl"])),
        "realized_pnl": optional_float(first_value(payload, ["realized_pnl", "realizedpnl"])),
        "history": [value for item in payload.get("history", []) if (value := optional_float(item)) is not None],
        "nav_history": [value for item in payload.get("nav_history", []) if (value := optional_float(item)) is not None],
        "return_history": [value for item in payload.get("return_history", []) if (value := optional_float(item)) is not None],
        "trend_period": str(payload.get("trend_period") or "30 DAYS").upper()[:16],
        "positions": positions,
    }


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    """Reject incomplete live data before it can overwrite the display."""
    if snapshot.get("net_liquidation") is None:
        raise DashboardError("Snapshot is missing a valid net liquidation value")
    if not str(snapshot.get("as_of") or "").strip():
        raise DashboardError("Snapshot is missing an as-of date")
    if not str(snapshot.get("currency") or "").strip():
        raise DashboardError("Snapshot is missing a base currency")


def load_json_source(source: str) -> dict[str, Any]:
    if source == "-":
        try:
            payload = json.loads(sys.stdin.readline())
        except json.JSONDecodeError as exc:
            raise DashboardError("Invalid JSON received on standard input") from exc
    elif source.startswith(("https://", "http://")):
        require_https_url(source, "IBKR_JSON_SOURCE", allow_local_http=True)
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


def flex_response_error(root: ET.Element) -> tuple[str, str]:
    status = next((str(node.text or "").strip() for node in root.iter() if xml_local_name(node.tag) == "status"), "")
    code = next((str(node.text or "").strip() for node in root.iter() if xml_local_name(node.tag) == "errorcode"), "")
    message = next((str(node.text or "").strip() for node in root.iter() if xml_local_name(node.tag) == "errormessage"), "")
    if status and status.lower() not in {"success", "succeeded"}:
        return code, message or "Flex report failed"
    if code and code not in {"0", "None"}:
        return code, message or "Flex report failed"
    return "", ""


def flex_reference(payload: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DashboardError("IBKR Flex returned invalid XML while starting the report") from exc
    error_code, error_text = flex_response_error(root)
    if error_code or error_text:
        suffix = f" (code {error_code})" if error_code else ""
        raise DashboardError(f"{error_text}{suffix}")
    code = next((node.text for node in root.iter() if xml_local_name(node.tag) == "referencecode"), None)
    if not code:
        raise DashboardError("IBKR Flex did not return a reference code")
    report_url = next((str(node.text or "").strip() for node in root.iter() if xml_local_name(node.tag) == "url"), "")
    if report_url:
        parsed = urllib.parse.urlsplit(report_url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".interactivebrokers.com"):
            raise DashboardError("IBKR Flex returned an unexpected report URL")
    return str(code), report_url


def flex_reference_code(payload: bytes) -> str:
    """Compatibility helper retained for callers that only need the code."""
    return flex_reference(payload)[0]


def parse_flex_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def flex_row_date(attrs: dict[str, Any]) -> date | None:
    return parse_flex_date(first_value(attrs, ["reportDate", "toDate", "periodEndDate", "date"]))


def sum_optional(values: Iterable[Any]) -> float | None:
    parsed = [number for value in values if (number := optional_float(value)) is not None]
    return sum(parsed) if parsed else None


def flex_is_daily(statement_attrs: dict[str, Any], change_rows: list[dict[str, Any]]) -> bool:
    period = str(first_value(statement_attrs, ["period", "dateRange"], "")).replace(" ", "").lower()
    if "lastbusinessday" in period:
        return True
    from_value = first_value(statement_attrs, ["fromDate"], first_value(change_rows[-1], ["fromDate"]) if change_rows else None)
    to_value = first_value(statement_attrs, ["toDate"], first_value(change_rows[-1], ["toDate"]) if change_rows else None)
    start, end = parse_flex_date(from_value), parse_flex_date(to_value)
    return bool(start and end and start == end)


def parse_flex_xml(payload: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DashboardError("IBKR Flex report is not valid XML; configure the query for XML output") from exc
    error_code, error_text = flex_response_error(root)
    if error_code or error_text:
        suffix = f" (code {error_code})" if error_code else ""
        raise DashboardError(f"{error_text}{suffix}")

    statements = [node for node in root.iter() if xml_local_name(node.tag) == "flexstatement"]
    if len(statements) != 1:
        raise DashboardError("Flex query must return exactly one account statement")
    statement_attrs = dict(statements[0].attrib)
    positions: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    change_rows: list[dict[str, Any]] = []
    mtm_rows: list[dict[str, Any]] = []
    currency = str(first_value(statement_attrs, ["currency", "baseCurrency"], env("DASHBOARD_CURRENCY", "USD")))
    for node in statements[0].iter():
        name = xml_local_name(node.tag)
        attrs = dict(node.attrib)
        if name in {"openposition", "position"}:
            positions.append(attrs)
        elif name in {"netassetvalue", "equitysummarybyreportdateinbase", "equitysummaryinbase"}:
            equity_rows.append(attrs)
            currency = str(first_value(attrs, ["currency", "currencyprimary", "basecurrency"], currency))
        elif name in {"cashreportcurrency", "cashreport"}:
            cash_rows.append(attrs)
        elif name == "changeinnav":
            change_rows.append(attrs)
        elif name in {"mtmperformancesummaryinbase", "marktomarketperformancesummaryinbase"}:
            mtm_rows.append(attrs)

    if not equity_rows and not change_rows:
        raise DashboardError("Flex XML is missing NAV Summary or Change in NAV data")

    dated_equity = [(flex_row_date(row), index, row) for index, row in enumerate(equity_rows)]
    dated_equity.sort(key=lambda item: (item[0] or date.min, item[1]))
    latest_equity = dated_equity[-1][2] if dated_equity else {}
    latest_change = change_rows[-1] if change_rows else {}
    nav_value = first_value(latest_equity, ["total", "netLiquidation", "nav", "endingValue"])
    if nav_value in (None, ""):
        nav_value = first_value(latest_change, ["endingValue", "netLiquidation", "total", "nav"])
    if optional_float(nav_value) is None:
        raise DashboardError("Flex XML did not contain a valid ending NAV")

    latest_date = flex_row_date(latest_equity) or flex_row_date(latest_change)
    if latest_date is None:
        latest_date = parse_flex_date(first_value(statement_attrs, ["toDate", "periodEndDate"]))
    if latest_date is None:
        raise DashboardError("Flex XML did not contain a report date")

    cash_value = first_value(latest_equity, ["cash"])
    if cash_value in (None, ""):
        base_cash_rows = [
            row for row in cash_rows
            if str(first_value(row, ["currency"], "")).upper() in {"BASE_SUMMARY", "BASE", currency.upper()}
        ]
        selected_cash = base_cash_rows[-1] if base_cash_rows else (cash_rows[-1] if cash_rows else {})
        cash_value = first_value(selected_cash, ["endingCash", "endingSettledCash"])

    is_daily = flex_is_daily(statement_attrs, change_rows)
    position_daily: dict[str, float] = {}
    if is_daily:
        for row in mtm_rows:
            symbol = str(first_value(row, ["symbol", "underlyingSymbol"], "")).strip()
            total = optional_float(first_value(row, ["total", "mtmPnl", "pnl"]))
            if symbol and total is not None:
                position_daily[symbol] = position_daily.get(symbol, 0.0) + total
    normalized_positions = []
    for row in positions:
        item = dict(row)
        symbol = str(first_value(item, ["symbol", "underlyingSymbol"], "")).strip()
        if symbol in position_daily:
            item["dailyPnl"] = position_daily[symbol]
        normalized_positions.append(item)

    nav_history = [
        optional_float(first_value(row, ["total", "netLiquidation", "nav", "endingValue"]))
        for _, _, row in dated_equity
    ]
    nav_history = [value for value in nav_history if value is not None][-30:]
    daily_pnl = first_value(latest_change, ["markToMarket", "mtm", "dailyPnl", "pnl"]) if is_daily else None
    payload_dict = {
        "as_of": latest_date.isoformat(),
        "currency": currency,
        "net_liquidation": nav_value,
        "cash": cash_value,
        "buying_power": None,
        "daily_pnl": daily_pnl,
        "unrealized_pnl": sum_optional(first_value(row, ["fifoPnlUnrealized", "unrealizedPnl", "unrealizedPL"]) for row in positions),
        "realized_pnl": sum_optional(first_value(row, ["fifoPnlRealized", "realizedPnl", "realizedPL"]) for row in positions),
        "positions": normalized_positions,
        "nav_history": nav_history,
    }
    return normalize_snapshot(payload_dict, "flex")


def flex_report_is_pending(payload: bytes) -> bool:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return False
    code, message = flex_response_error(root)
    return code == "1019" or "generation in progress" in message.lower()


def flex_report_url(base: str, returned_url: str, reference_code: str, token: str) -> str:
    endpoint = returned_url or f"{base}/GetStatement"
    separator = "&" if urllib.parse.urlsplit(endpoint).query else "?"
    return f"{endpoint}{separator}{urllib.parse.urlencode({'q': reference_code, 't': token, 'v': '3'})}"


def fetch_flex_report(query_id: str, token: str, *, period_days: str = "") -> bytes:
    base = env("IBKR_FLEX_BASE_URL", "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService").rstrip("/")
    require_https_url(base, "IBKR_FLEX_BASE_URL")
    parameters: dict[str, str] = {"q": query_id, "t": token, "v": "3"}
    if period_days:
        try:
            days = int(period_days)
        except ValueError as exc:
            raise DashboardError("IBKR_FLEX_PERIOD_DAYS must be an integer from 1 to 365") from exc
        if not 1 <= days <= 365:
            raise DashboardError("IBKR_FLEX_PERIOD_DAYS must be an integer from 1 to 365")
        parameters["p"] = str(days)
    start_url = f"{base}/SendRequest?{urllib.parse.urlencode(parameters)}"
    reference_code, returned_url = flex_reference(request_bytes(start_url, attempts=3))
    report_url = flex_report_url(base, returned_url, reference_code, token)
    try:
        poll_attempts = max(1, int(env("IBKR_FLEX_POLL_ATTEMPTS", "8")))
    except ValueError as exc:
        raise DashboardError("IBKR_FLEX_POLL_ATTEMPTS must be an integer") from exc
    report = b""
    for attempt in range(poll_attempts):
        if attempt:
            time.sleep(min(2**attempt, 8))
        report = request_bytes(report_url, attempts=3)
        if not flex_report_is_pending(report):
            break
    if flex_report_is_pending(report):
        raise DashboardError("IBKR Flex report was still pending after the polling limit")
    if not report.lstrip().startswith(b"<"):
        raise DashboardError("IBKR Flex query must use XML output")
    return report


def load_flex_source() -> dict[str, Any]:
    query_id = env("IBKR_FLEX_QUERY_ID")
    token = secret("IBKR_FLEX_TOKEN", IBKR_FLEX_KEYCHAIN_SERVICE)
    if not query_id or not token:
        raise DashboardError("IBKR_FLEX_QUERY_ID and IBKR_FLEX_TOKEN are required for Flex mode")
    snapshot = parse_flex_xml(fetch_flex_report(query_id, token, period_days=env("IBKR_FLEX_PERIOD_DAYS", "30")))
    daily_query_id = env("IBKR_FLEX_DAILY_QUERY_ID")
    if daily_query_id:
        daily = parse_flex_xml(fetch_flex_report(daily_query_id, token))
        for key in ("daily_pnl", "daily_pnl_pct"):
            snapshot[key] = daily[key]
        daily_by_symbol = {position["symbol"]: position for position in daily["positions"]}
        for position in snapshot["positions"]:
            daily_position = daily_by_symbol.get(position["symbol"])
            if daily_position:
                position["daily_pnl"] = daily_position["daily_pnl"]
                position["daily_pct"] = daily_position["daily_pct"]
    validate_snapshot(snapshot)
    return snapshot


def load_client_portal_source() -> dict[str, Any]:
    base = env("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api").rstrip("/")
    require_https_url(base, "IBKR_CP_BASE_URL")
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
    ledger_timestamp = optional_float(first_value(base_ledger, ["timestamp"]))
    snapshot = normalize_snapshot(
        {
            "as_of": datetime.fromtimestamp(ledger_timestamp, tz=ZoneInfo("UTC")).isoformat() if ledger_timestamp else None,
            "currency": currency,
            "net_liquidation": first_value(account_pnl, ["nl"], first_value(base_ledger, ["netliquidationvalue"])),
            "cash": first_value(base_ledger, ["cashbalance", "settledcash"]),
            "daily_pnl": first_value(account_pnl, ["dpl"]),
            "unrealized_pnl": first_value(account_pnl, ["upl"], first_value(base_ledger, ["unrealizedpnl"])),
            "realized_pnl": first_value(base_ledger, ["realizedpnl"]),
            "positions": positions if isinstance(positions, list) else [],
        },
        "client_portal",
    )
    validate_snapshot(snapshot)
    return snapshot


def fetch_snapshot() -> dict[str, Any]:
    source = env("IBKR_SOURCE", "flex").lower()
    if source == "json":
        location = env("IBKR_JSON_SOURCE")
        if not location:
            raise DashboardError("IBKR_JSON_SOURCE is required in run mode; sample fallback is disabled")
        snapshot = load_json_source(location)
        validate_snapshot(snapshot)
        return snapshot
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
    raise DashboardError("No usable TrueType font was found; install DejaVu Sans or set DASHBOARD_FONT_PATH")


def currency_prefix(currency: str) -> str:
    return {"USD": "$", "CNY": "CN¥", "HKD": "HK$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency, f"{currency} ")


def money(value: float | None, currency: str, compact: bool = False, signed: bool = False) -> str:
    if value is None:
        return "N/A"
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
    pnl: float | None,
    box: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = box
    positive = pnl is not None and pnl > 0
    foreground = 255 if positive else 0
    background = 0 if positive else 255
    draw.rounded_rectangle(box, radius=3, fill=background, outline=0, width=1)

    middle = (top + bottom) // 2
    label = "N/A" if pnl is None else (f"{pnl:+,.2f}" if pnl else "0.00")
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    icon_width = 16
    gap = 5
    group_width = icon_width + gap + text_width
    group_left = left + ((right - left + 1) - group_width) // 2

    if pnl is None:
        trend_points = [(group_left, middle), (group_left + 16, middle)]
    elif positive:
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
    timezone = dashboard_timezone(timezone_name)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            report_date = date.fromisoformat(value)
        except ValueError:
            return "AS OF UNKNOWN"
        return f"AS OF {report_date:%m/%d}"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "TIME UNKNOWN"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    timestamp = timestamp.astimezone(timezone)
    offset = timestamp.utcoffset() or timedelta(0)
    total_minutes = round(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    offset_label = f"UTC{sign}{hours}" if not minutes else f"UTC{sign}{hours}:{minutes:02d}"
    return f"{offset_label} {timestamp:%m/%d %H:%M}"


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

    raw_as_of = str(snapshot["as_of"])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_as_of):
        sample_day = raw_as_of
    else:
        timezone = dashboard_timezone()
        try:
            timestamp = datetime.fromisoformat(raw_as_of.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone)
            sample_day = timestamp.astimezone(timezone).date().isoformat()
        except ValueError as exc:
            raise DashboardError("Snapshot has an invalid as-of timestamp") from exc
    history = [
        item
        for item in history
        if str(item.get("date") or item.get("at", ""))[:10] != sample_day
    ]
    history.append({"date": sample_day, "net_liquidation": snapshot["net_liquidation"]})
    history = history[-limit:]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(history, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
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

    if len(nav_values) < 2:
        message = "COLLECTING HISTORY"
        width = draw.textbbox((0, 0), message, font=font)[2]
        draw.text((left + (right - left - width) / 2, top + (bottom - top) / 2 - 5), message, font=font, fill=0)
    else:
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
    day_percent = "N/A" if snapshot["daily_pnl_pct"] is None else f"{snapshot['daily_pnl_pct']:+.2f}%"
    draw_eink_text(draw, (12, 120), f"DAY %  {day_percent}", font=regular_10, fill=0)

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
    positions = sorted(snapshot["positions"], key=lambda item: abs(item["market_value"] or 0.0), reverse=True)[:max_positions]
    if not positions:
        draw.text((18, 182), "No open positions in this snapshot", font=regular_12, fill=0)
    for index, position in enumerate(positions):
        row_top = 176 + index * 23
        if row_top > 250:
            break
        text_y = row_top + 2
        draw.text((18, text_y), position["symbol"], font=bold_12, fill=0)
        draw_eink_text(draw, (110, text_y), quantity_label(position["quantity"]), font=regular_12, fill=0)
        price_label = "N/A" if position["market_price"] is None else f"{position['market_price']:,.2f}"
        draw_eink_text(draw, (188, text_y), price_label, font=regular_12, fill=0)
        draw_pnl_badge(draw, position["daily_pnl"], (286, row_top, 382, row_top + 19), regular_10)
        if index < len(positions) - 1:
            draw.line((18, row_top + 21, 382, row_top + 21), fill=190, width=1)

    footer = (
        f"CASH {money(snapshot['cash'], currency, compact=True)}"
        f" · BUYING POWER {money(snapshot['buying_power'], currency, compact=True)}"
    )
    draw_eink_text(draw, (12, 280), footer, font=regular_10, fill=0)
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
    require_https_url(base, "ZECTRIX_API_BASE_URL")
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
    require_https_url(base, "ZECTRIX_API_BASE_URL")
    page_id = env("ZECTRIX_PAGE_ID", "1")
    body, boundary = multipart_image(image_path, page_id)
    url = f"{base}/open/v1/devices/{urllib.parse.quote(device_id, safe='')}/display/image"
    try:
        attempts = max(1, int(env("ZECTRIX_PUSH_ATTEMPTS", "3")))
    except ValueError as exc:
        raise DashboardError("ZECTRIX_PUSH_ATTEMPTS must be an integer") from exc
    response = request_bytes(
        url,
        method="POST",
        headers={"X-API-Key": api_key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body,
        attempts=attempts,
    )
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise DashboardError("ZECTRIX returned an invalid response") from exc
    if not isinstance(parsed, dict):
        raise DashboardError("ZECTRIX returned an unexpected response")
    if parsed.get("code") not in (None, 0):
        raise DashboardError(f"ZECTRIX rejected the image: code {parsed.get('code')}")


def snapshot_fingerprint(snapshot: dict[str, Any], history: list[float] | None = None) -> str:
    """Hash display data while deliberately ignoring the retrieval timestamp."""
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
        "history": history if history is not None else snapshot.get("nav_history") or snapshot.get("history", []),
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
    fingerprint = payload.get("sha256") if isinstance(payload, dict) else None
    return fingerprint if isinstance(fingerprint, str) else ""


def write_last_push_fingerprint(path: Path, fingerprint: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sha256": fingerprint, "pushed_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def ping_healthcheck(success: bool) -> None:
    url = env("HEALTHCHECK_URL")
    if not url:
        return
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        print("warning: HEALTHCHECK_URL must be an HTTPS URL", file=sys.stderr)
        return
    target = url.rstrip("/") if success else f"{url.rstrip('/')}/fail"
    try:
        request_bytes(target, timeout=10)
    except DashboardError as exc:
        print(f"warning: healthcheck notification failed: {exc}", file=sys.stderr)


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


def command_doctor(args: argparse.Namespace) -> None:
    source = env("IBKR_SOURCE", "flex").lower()
    issues: list[str] = []
    if source == "json":
        location = env("IBKR_JSON_SOURCE")
        if not location:
            issues.append("IBKR_JSON_SOURCE is missing; run mode never falls back to sample data")
        elif location.startswith(("https://", "http://")):
            try:
                require_https_url(location, "IBKR_JSON_SOURCE", allow_local_http=True)
            except DashboardError as exc:
                issues.append(str(exc))
        elif not Path(location).expanduser().exists():
            issues.append(f"JSON source does not exist: {location}")
    elif source == "flex":
        if not env("IBKR_FLEX_QUERY_ID"):
            issues.append("IBKR_FLEX_QUERY_ID is missing")
        if not secret("IBKR_FLEX_TOKEN", IBKR_FLEX_KEYCHAIN_SERVICE):
            issues.append("IBKR_FLEX_TOKEN is missing")
    elif source == "client_portal":
        base = urllib.parse.urlsplit(env("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api"))
        if base.scheme != "https" or not base.hostname:
            issues.append("IBKR_CP_BASE_URL must be an HTTPS URL")
    else:
        issues.append("IBKR_SOURCE must be json, flex, or client_portal")
    try:
        dashboard_timezone()
    except DashboardError as exc:
        issues.append(str(exc))

    print(f"IBKR source: {source}")
    print(f"Sample snapshot: {SAMPLE_SNAPSHOT}")
    print(f"ZECTRIX credential: {'present' if secret('ZECTRIX_API_KEY', ZECTRIX_KEYCHAIN_SERVICE) else 'missing (preview only)'}")
    if issues:
        raise DashboardError("; ".join(issues))
    print("Static configuration checks passed")
    if args.probe:
        snapshot = fetch_snapshot()
        print("Live source probe passed")
        print(
            "Recognized fields: "
            f"nav=yes cash={'yes' if snapshot['cash'] is not None else 'no'} "
            f"daily_pnl={'yes' if snapshot['daily_pnl'] is not None else 'no'} "
            f"positions={'yes' if snapshot['positions'] else 'no'}"
        )


def command_run(args: argparse.Namespace) -> None:
    try:
        snapshot = fetch_snapshot()
        validate_snapshot(snapshot)
        state_path = Path(env("DASHBOARD_STATE_PATH", "state/history.json")).expanduser()
        if len(snapshot.get("nav_history", [])) >= 2:
            history = snapshot["nav_history"][-30:]
        else:
            history = append_history(snapshot, state_path)
        if len(history) < 2 and snapshot.get("history"):
            history = snapshot["history"]
        output = Path(args.output or env("DASHBOARD_OUTPUT_PATH", "output/ibkr-dashboard.png")).expanduser()
        render(snapshot, output, history)
        if args.no_push:
            print(f"Dashboard rendered without push: {output}")
        else:
            fingerprint = snapshot_fingerprint(snapshot, history)
            dedupe_path = Path(env("DASHBOARD_DEDUPE_STATE_PATH", "state/last-push.json")).expanduser()
            if not args.force and not args.no_dedupe and read_last_push_fingerprint(dedupe_path) == fingerprint:
                print("Dashboard unchanged: push skipped")
            else:
                push_zectrix(output)
                write_last_push_fingerprint(dedupe_path, fingerprint)
                print(f"Dashboard pushed: {output}")
    except (DashboardError, OSError, ValueError):
        ping_healthcheck(False)
        raise
    ping_healthcheck(True)


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
    run.add_argument("--force", action="store_true", help="Push even when display data is unchanged")
    run.add_argument("--no-dedupe", action="store_true", help="Disable unchanged-display push suppression")
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
    doctor.add_argument("--probe", action="store_true", help="Fetch once and report field availability without printing values")
    doctor.set_defaults(handler=command_doctor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        load_env_file(args.env_file)
        args.handler(args)
    except (DashboardError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
