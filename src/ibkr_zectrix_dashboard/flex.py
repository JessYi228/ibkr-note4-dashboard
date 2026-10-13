"""IBKR Flex Web Service client and Activity Flex XML parser.

Parsing rules are adapted from ibkr-note4-dashboard (MIT, see NOTICE). Flex
data is a broker report: it is dated by IBKR's report date, never by the time
we fetched it.
"""

from __future__ import annotations

import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any, Callable, Iterable

from .errors import DashboardError, FlexRateLimitError
from .http import request_bytes
from .snapshot import Position, Snapshot, optional_float

FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_MIN_INTERVAL_SECONDS = 1.1
RATE_LIMIT_RETRY_SECONDS = 61.0
POLL_ATTEMPTS = 10

# Error codes whose meaning the user should see on the display.
KNOWN_ERRORS = {
    "1003": "Flex statement not available yet",
    "1012": "Flex token expired - generate a new one",
    "1015": "Flex token invalid",
    "1018": "Flex rate limit",
    "1019": "Flex statement still generating",
    "1020": "Flex query invalid",
}

Fetcher = Callable[[str], bytes]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first(mapping: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def _text(root: ET.Element, name: str) -> str:
    return next((str(node.text or "").strip() for node in root.iter() if _local(node.tag) == name), "")


def response_error(root: ET.Element) -> tuple[str, str]:
    status, code, message = _text(root, "status"), _text(root, "errorcode"), _text(root, "errormessage")
    if (status and status.lower() not in {"success", "succeeded"}) or (code and code not in {"0", "None"}):
        return code, KNOWN_ERRORS.get(code) or message or "Flex request failed"
    return "", ""


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip().split(";", 1)[0].split(",", 1)[0]
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _row_date(attrs: dict[str, Any]) -> date | None:
    return parse_date(_first(attrs, ["reportDate", "toDate", "periodEndDate", "date"]))


def _sum(values: Iterable[Any]) -> float | None:
    parsed = [number for value in values if (number := optional_float(value)) is not None]
    return sum(parsed) if parsed else None


def _is_single_day(statement: dict[str, Any], change_rows: list[dict[str, Any]]) -> bool:
    period = str(_first(statement, ["period", "dateRange"], "")).replace(" ", "").lower()
    if "lastbusinessday" in period:
        return True
    last_change = change_rows[-1] if change_rows else {}
    start = parse_date(_first(statement, ["fromDate"], _first(last_change, ["fromDate"])))
    end = parse_date(_first(statement, ["toDate"], _first(last_change, ["toDate"])))
    return bool(start and end and start == end)


def parse_statement(payload: bytes) -> tuple[Snapshot, dict[str, float]]:
    """Return the snapshot plus per-symbol daily P&L (only for single-day reports)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DashboardError("Flex report is not XML; set the query output format to XML") from exc
    code, message = response_error(root)
    if code or message:
        raise DashboardError(f"{message} (code {code})" if code else message)

    statements = [node for node in root.iter() if _local(node.tag) == "flexstatement"]
    if len(statements) != 1:
        raise DashboardError("Flex query must return exactly one account statement")
    statement = dict(statements[0].attrib)
    positions: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    change_rows: list[dict[str, Any]] = []
    mtm_rows: list[dict[str, Any]] = []
    currency = str(_first(statement, ["currency", "baseCurrency"], "USD"))
    for node in statements[0].iter():
        name, attrs = _local(node.tag), dict(node.attrib)
        if name in {"openposition", "position"}:
            # Summary rows duplicate lot rows when the query includes lots.
            if str(_first(attrs, ["levelOfDetail"], "SUMMARY")).upper() == "SUMMARY":
                positions.append(attrs)
        elif name in {"netassetvalue", "equitysummarybyreportdateinbase", "equitysummaryinbase"}:
            equity_rows.append(attrs)
            currency = str(_first(attrs, ["currency", "currencyPrimary", "baseCurrency"], currency))
        elif name in {"cashreportcurrency", "cashreport"}:
            cash_rows.append(attrs)
        elif name == "changeinnav":
            change_rows.append(attrs)
        elif name in {"mtmperformancesummaryinbase", "marktomarketperformancesummaryinbase", "mtmperformancesummaryunderlying"}:
            mtm_rows.append(attrs)

    if not equity_rows and not change_rows:
        raise DashboardError("Flex report is missing the NAV Summary / Change in NAV sections")

    dated = sorted(
        ((day, index, row) for index, row in enumerate(equity_rows) if (day := _row_date(row)) is not None),
        key=lambda item: (item[0], item[1]),
    )
    latest_equity = dated[-1][2] if dated else {}
    latest_change = change_rows[-1] if change_rows else {}
    nav = optional_float(_first(latest_equity, ["total", "netLiquidation", "endingValue"]))
    if nav is None:
        nav = optional_float(_first(latest_change, ["endingValue", "netLiquidation", "total"]))
    if nav is None:
        raise DashboardError("Flex report has no valid ending NAV")

    report_date = (dated[-1][0] if dated else None) or _row_date(latest_change)
    if report_date is None:
        report_date = parse_date(_first(statement, ["toDate", "periodEndDate"]))
    if report_date is None:
        raise DashboardError("Flex report has no report date")

    cash = optional_float(_first(latest_equity, ["cash"]))
    if cash is None and cash_rows:
        base_rows = [r for r in cash_rows if str(_first(r, ["currency"], "")).upper() in {"BASE_SUMMARY", "BASE", currency.upper()}]
        cash = optional_float(_first((base_rows or cash_rows)[-1], ["endingCash", "endingSettledCash"]))

    single_day = _is_single_day(statement, change_rows)
    per_symbol: dict[str, float] = {}
    if single_day:
        for row in mtm_rows:
            symbol = str(_first(row, ["symbol", "underlyingSymbol"], "")).strip()
            total = optional_float(_first(row, ["total", "mtmPnl"]))
            if symbol and total is not None:
                per_symbol[symbol] = per_symbol.get(symbol, 0.0) + total

    history: dict[date, float] = {}
    for day, _, row in dated:
        value = optional_float(_first(row, ["total", "netLiquidation", "endingValue"]))
        if value is not None:
            history[day] = value

    snapshot = Snapshot(
        report_date=report_date,
        currency=currency.upper(),
        net_liquidation=nav,
        cash=cash,
        daily_pnl=optional_float(_first(latest_change, ["markToMarket", "mtm"])) if single_day else None,
        unrealized_pnl=_sum(_first(row, ["fifoPnlUnrealized", "unrealizedPnl"]) for row in positions),
        positions=[
            Position(
                symbol=str(_first(row, ["symbol", "underlyingSymbol"], "?")).strip()[:12],
                quantity=optional_float(_first(row, ["position", "quantity"])) or 0.0,
                market_price=optional_float(_first(row, ["markPrice", "marketPrice"])),
                market_value=optional_float(_first(row, ["positionValue", "marketValue"])),
                daily_pnl=per_symbol.get(str(_first(row, ["symbol"], "")).strip()) if single_day else None,
                unrealized_pnl=optional_float(_first(row, ["fifoPnlUnrealized", "unrealizedPnl"])),
            )
            for row in positions
        ],
        nav_history=sorted(history.items())[-30:],
    )
    snapshot.validate()
    return snapshot, per_symbol


class FlexClient:
    """Two-step Flex download (SendRequest -> GetStatement) with IBKR pacing."""

    def __init__(self, token: str, *, base_url: str = FLEX_BASE_URL, fetch: Fetcher | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise DashboardError("IBKR_FLEX_TOKEN is not configured")
        self._token = token
        self._base = base_url.rstrip("/")
        self._fetch = fetch or (lambda url: request_bytes(url, attempts=3))
        self._sleep = sleep
        self._last_send = 0.0

    def _url(self, endpoint: str, params: dict[str, str]) -> str:
        separator = "&" if urllib.parse.urlsplit(endpoint).query else "?"
        return f"{endpoint}{separator}{urllib.parse.urlencode({**params, 't': self._token, 'v': '3'})}"

    def _start(self, query_id: str) -> tuple[str, str]:
        for attempt in range(2):
            wait = SEND_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_send)
            if wait > 0:
                self._sleep(wait)
            payload = self._fetch(self._url(f"{self._base}/SendRequest", {"q": query_id}))
            self._last_send = time.monotonic()
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise DashboardError("Flex SendRequest returned invalid XML") from exc
            code, message = response_error(root)
            if code == "1018" and attempt == 0:
                self._sleep(RATE_LIMIT_RETRY_SECONDS)
                continue
            if code or message:
                error = FlexRateLimitError if code == "1018" else DashboardError
                raise error(f"{message} (code {code})" if code else message)
            reference = _text(root, "referencecode")
            if not reference:
                raise DashboardError("Flex SendRequest returned no reference code")
            report_url = _text(root, "url")
            if report_url:
                host = urllib.parse.urlsplit(report_url).hostname or ""
                if not report_url.startswith("https://") or not host.endswith(".interactivebrokers.com"):
                    raise DashboardError("Flex returned an unexpected report URL")
            return reference, report_url or f"{self._base}/GetStatement"
        raise DashboardError("Flex SendRequest did not succeed")

    def download(self, query_id: str) -> bytes:
        if not query_id:
            raise DashboardError("IBKR_FLEX_QUERY_ID is not configured")
        reference, endpoint = self._start(query_id)
        url = self._url(endpoint, {"q": reference})
        for attempt in range(POLL_ATTEMPTS):
            if attempt:
                self._sleep(min(5 * attempt, 30))
            payload = self._fetch(url)
            try:
                code, _ = response_error(ET.fromstring(payload))
            except ET.ParseError:
                code = ""
            if code != "1019":
                return payload
        raise DashboardError("Flex statement still generating after polling (code 1019)")


def fetch_snapshot(client: FlexClient, query_id: str, daily_query_id: str = "") -> Snapshot:
    snapshot, _ = parse_statement(client.download(query_id))
    if daily_query_id:
        daily, per_symbol = parse_statement(client.download(daily_query_id))
        # Only merge when both reports describe the same session.
        if daily.report_date == snapshot.report_date:
            snapshot.daily_pnl = daily.daily_pnl
            for position in snapshot.positions:
                position.daily_pnl = per_symbol.get(position.symbol)
    return snapshot
