from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ibkr_zectrix_dashboard.config import RefreshPolicy
from ibkr_zectrix_dashboard.errors import DashboardError
from ibkr_zectrix_dashboard.refresh import run_once
from ibkr_zectrix_dashboard.snapshot import Snapshot
from ibkr_zectrix_dashboard.state import State, StateStore

BJ = ZoneInfo("Asia/Shanghai")
POLICY = RefreshPolicy(ready_after=timedelta(minutes=60), stale_after=timedelta(minutes=600),
                       retry_every=timedelta(minutes=25))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ)


def snap(day: date, nav: float = 4000.0) -> Snapshot:
    return Snapshot(report_date=day, currency="USD", net_liquidation=nav, cash=10.0, daily_pnl=None,
                    unrealized_pnl=None, nav_history=[(day, nav)])


class Harness:
    def __init__(self):
        self.state = State()
        self.reports: list[Snapshot | Exception] = []
        self.fetches = 0
        self.shown: list[tuple[date, str]] = []

    def fetch(self) -> Snapshot:
        self.fetches += 1
        item = self.reports.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def show(self, snapshot, banner) -> None:
        self.shown.append((snapshot.report_date, banner.status))

    def tick(self, now: datetime, **kwargs):
        return run_once(self.state, now=now, policy=POLICY, fetch=self.fetch, show=self.show, **kwargs)


def test_first_run_fetches_and_pushes():
    h = Harness()
    h.reports = [snap(date(2026, 10, 9))]
    outcome = h.tick(at(12, 18, 17))
    assert outcome.action == "fetched" and outcome.pushed
    assert h.shown == [(date(2026, 10, 9), "ok")]


def test_up_to_date_makes_no_network_calls():
    h = Harness()
    h.reports = [snap(date(2026, 10, 9))]
    h.tick(at(12, 18, 17))
    for hour in (19, 20, 23):
        assert h.tick(at(12, hour)).action == "up_to_date"
    assert h.fetches == 1 and len(h.shown) == 1


def test_monday_report_arrives_tuesday_morning():
    h = Harness()
    h.reports = [snap(date(2026, 10, 9))]
    h.tick(at(12, 18, 17))
    # Report not published at 05:00, retry spacing honoured, appears at 06:00.
    h.reports = [snap(date(2026, 10, 9)), snap(date(2026, 10, 12), 4050)]
    assert h.tick(at(13, 5, 0)).action == "fetched"
    assert h.tick(at(13, 5, 10)).action == "waiting"
    outcome = h.tick(at(13, 6, 0))
    assert outcome.pushed and h.shown[-1] == (date(2026, 10, 12), "ok")
    assert h.state.first_seen["2026-10-12"].startswith("2026-10-13T06:00")
    assert h.fetches == 3


def test_failures_become_a_visible_stale_screen_once():
    h = Harness()
    h.reports = [snap(date(2026, 10, 9))]
    h.tick(at(12, 18, 17))
    h.reports = [DashboardError("Flex token expired - generate a new one (code 1012)")] * 30
    # Before the 14:00 Beijing deadline: failure recorded, screen untouched.
    outcome = h.tick(at(13, 5, 0))
    assert outcome.failed and not outcome.pushed
    # After the deadline: one STALE push, then quiet while failures continue.
    outcome = h.tick(at(13, 14, 0))
    assert outcome.failed and outcome.pushed and h.shown[-1] == (date(2026, 10, 9), "stale")
    assert not h.tick(at(13, 14, 30)).pushed
    assert h.state.consecutive_failures == 3
    # Fixing the token: next run fetches and clears the warning.
    h.reports = [snap(date(2026, 10, 12), 4050)]
    outcome = h.tick(at(13, 15, 0))
    assert outcome.pushed and h.shown[-1] == (date(2026, 10, 12), "ok")
    assert h.state.last_error is None and h.state.consecutive_failures == 0


def test_stale_banner_counts_missing_sessions():
    h = Harness()
    banners = []
    h.show = lambda snapshot, banner: banners.append(banner)
    h.reports = [snap(date(2026, 10, 8))]
    h.tick(at(14, 15, 0))  # expected 10/13; have 10/8 -> 10/9, 10/12, 10/13 missing
    assert banners[-1].status == "stale" and banners[-1].sessions_behind == 3


def test_older_report_never_replaces_newer_data():
    h = Harness()
    h.reports = [snap(date(2026, 10, 12))]
    h.tick(at(13, 6, 0))
    h.reports = [snap(date(2026, 10, 9))]
    outcome = h.tick(at(13, 7, 0), force=True)
    assert outcome.failed and h.state.last_good.report_date == date(2026, 10, 12)


def test_push_failure_keeps_fetched_data_and_retries_push():
    h = Harness()
    h.reports = [snap(date(2026, 10, 12))]

    def broken(snapshot, banner):
        raise DashboardError("HTTP 503 from cloud.zectrix.com")

    h.show = broken
    with pytest.raises(DashboardError):
        h.tick(at(13, 6, 0))
    assert h.state.last_good is not None and h.state.displayed_fingerprint is None
    h.show = lambda snapshot, banner: h.shown.append((snapshot.report_date, banner.status))
    outcome = h.tick(at(13, 6, 10))
    assert outcome.action == "up_to_date" and outcome.pushed and h.fetches == 1


def test_state_round_trip(tmp_path):
    h = Harness()
    h.reports = [snap(date(2026, 10, 12))]
    h.tick(at(13, 6, 0))
    store = StateStore(tmp_path / "state")
    store.save(h.state)
    assert (store.path.stat().st_mode & 0o777) == 0o600
    loaded = store.load()
    assert loaded.last_good.report_date == date(2026, 10, 12)
    assert loaded.displayed_fingerprint == h.state.displayed_fingerprint
    assert loaded.first_seen == h.state.first_seen
    assert replace(loaded, last_good=None) == replace(h.state, last_good=None)
