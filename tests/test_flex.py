from datetime import date
from pathlib import Path

import pytest

from ibkr_zectrix_dashboard.errors import DashboardError
from ibkr_zectrix_dashboard.flex import FlexClient, fetch_snapshot, parse_statement

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "secret-token-123"


def xml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def flex_reply(code: str = "", message: str = "", reference: str = "") -> bytes:
    if code:
        return (f"<FlexStatementResponse><Status>Warn</Status><ErrorCode>{code}</ErrorCode>"
                f"<ErrorMessage>{message}</ErrorMessage></FlexStatementResponse>").encode()
    return (f"<FlexStatementResponse><Status>Success</Status><ReferenceCode>{reference}</ReferenceCode>"
            "<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
            "</FlexStatementResponse>").encode()


class FakeFlex:
    def __init__(self, statements: dict[str, list[bytes]]):
        self.statements = statements
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if "/SendRequest" in url:
            query = url.split("q=", 1)[1].split("&", 1)[0]
            return flex_reply(reference=f"ref-{query}")
        reference = url.split("q=", 1)[1].split("&", 1)[0]
        return self.statements[reference].pop(0)


def test_parse_30_day_statement():
    snapshot, per_symbol = parse_statement(xml("flex_30d.xml"))
    assert snapshot.report_date == date(2026, 10, 12)
    assert snapshot.net_liquidation == pytest.approx(4201.90)
    assert snapshot.cash == pytest.approx(312.08)
    assert snapshot.daily_pnl is None  # 30-day mark-to-market is not a daily P&L
    assert [p.symbol for p in snapshot.positions] == ["SPY", "QQQ"]  # LOT row ignored
    assert snapshot.unrealized_pnl == pytest.approx(99.11 + 55.35)
    assert snapshot.nav_history[-1] == (date(2026, 10, 12), pytest.approx(4201.90))
    assert len(snapshot.nav_history) == 4
    assert snapshot.window_days == 29 and snapshot.window_label == "29D"  # 09/14..10/12 calendar days
    assert per_symbol == {}


def test_daily_query_supplies_daily_pnl():
    fake = FakeFlex({"ref-111": [xml("flex_30d.xml")], "ref-222": [xml("flex_daily.xml")]})
    client = FlexClient(TOKEN, fetch=fake, sleep=lambda _: None)
    snapshot = fetch_snapshot(client, "111", "222")
    assert snapshot.daily_pnl == pytest.approx(15.48)
    assert {p.symbol: p.daily_pnl for p in snapshot.positions} == {"SPY": 10.71, "QQQ": 4.77}
    assert snapshot.daily_pnl_pct == pytest.approx(15.48 / 4186.42 * 100)


def test_polls_while_statement_is_generating():
    pending = flex_reply("1019", "Statement generation in progress. Please try again shortly.")
    fake = FakeFlex({"ref-111": [pending, pending, xml("flex_30d.xml")]})
    sleeps: list[float] = []
    client = FlexClient(TOKEN, fetch=fake, sleep=sleeps.append)
    snapshot = fetch_snapshot(client, "111")
    assert snapshot.report_date == date(2026, 10, 12)
    assert sum(1 for url in fake.urls if "GetStatement" in url) == 3


def test_expired_token_error_is_readable_and_secret_free():
    def fetch(url: str) -> bytes:
        return flex_reply("1012", "Token has expired.")

    client = FlexClient(TOKEN, fetch=fetch, sleep=lambda _: None)
    with pytest.raises(DashboardError) as info:
        client.download("111")
    assert "expired" in str(info.value)
    assert TOKEN not in str(info.value)


def test_rejects_multi_account_and_csv():
    with pytest.raises(DashboardError, match="not XML"):
        parse_statement(b"ClientAccountID,NAV\n")
    doubled = xml("flex_30d.xml").replace(b'<FlexStatements count="1">', b'<FlexStatements count="2"><FlexStatement accountId="U1"/>')
    with pytest.raises(DashboardError, match="exactly one"):
        parse_statement(doubled)


def test_unexpected_report_host_is_refused():
    def fetch(url: str) -> bytes:
        return (b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>1</ReferenceCode>"
                b"<Url>https://evil.example.com/GetStatement</Url></FlexStatementResponse>")

    with pytest.raises(DashboardError, match="unexpected report URL"):
        FlexClient(TOKEN, fetch=fetch, sleep=lambda _: None).download("111")
