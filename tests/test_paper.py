import io
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from ibkr_zectrix_dashboard.errors import DashboardError
from ibkr_zectrix_dashboard.paper import PaperBook, collect, parse_book, parse_books, parse_labels, run_paper_once
from ibkr_zectrix_dashboard.render_paper import render_paper_png

TZ = ZoneInfo("Asia/Shanghai")
# Wednesday 10/14 runners commit ~21:35Z; at 10/15 02:00Z the 10/14 session is due (close + 4h).
NOW = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
BOOKS = [("PROD", "http://127.0.0.1:1"), ("LAB", "http://127.0.0.1:2")]


def marks(account_id: str, closes: list[tuple[str, float]]) -> list[dict]:
    rows = []
    for session, value in closes:
        rows.append({"account_id": account_id, "session": session, "timestamp": f"{session}T13:30:00Z",
                     "equity": str(value - 50)})  # open mark must be ignored
        rows.append({"account_id": account_id, "session": session, "timestamp": f"{session}T20:00:00Z",
                     "equity": str(value)})
    return rows


def status(last: str = "2026-10-14", runner: str = "ok", mode: str = "PAPER_ONLY", halted: bool = False) -> dict:
    series = {
        "benchmark_spy": [("2026-10-12", 100000), ("2026-10-13", 100500), ("2026-10-14", 101000)],
        "alpha_v1": [("2026-10-12", 100000), ("2026-10-13", 102000), ("2026-10-14", 101490)],
    }
    return {
        "mode": mode,
        "health": {"last_session": last, "runner_status": runner, "backlog_count": 0, "heartbeat_stale": False},
        "accounts": [
            {"account_id": key, "equity": str(values[-1][1]), "initial_cash": "100000.00", "halted": halted}
            for key, values in series.items()
        ],
        "equity": {key: marks(key, values) for key, values in series.items()},
    }


def test_parse_book_separates_benchmarks_and_uses_close_marks():
    book = parse_book("PROD", status())
    assert book.benchmarks == {"SPY": pytest.approx(1.0)}
    (account,) = book.accounts
    assert account.label == "ALPHA V1"
    assert account.closes == [100000, 102000, 101490]
    assert account.total_pct == pytest.approx(1.49)
    assert account.day_pct == pytest.approx((101490 / 102000 - 1) * 100)
    assert book.start == date(2026, 10, 12) and book.last_session == date(2026, 10, 14)
    assert book.problem is None


def test_refuses_non_paper_books_and_remote_urls():
    with pytest.raises(DashboardError, match="PAPER_ONLY"):
        parse_book("X", status(mode="LIVE"))
    with pytest.raises(DashboardError, match="loopback"):
        parse_books("PROD=http://example.com:9001")
    assert parse_books("prod=http://127.0.0.1:9001/") == [("PROD", "http://127.0.0.1:9001")]
    with pytest.raises(DashboardError, match="not set"):
        parse_books("")


def test_collect_flags_lagging_unhealthy_and_unreachable_books():
    previous = {"LAB": parse_book("LAB", status())}

    def fetch(url: str) -> dict:
        if url.startswith("http://127.0.0.1:2"):
            raise DashboardError("127.0.0.1:2 unreachable")
        return status(last="2026-10-13")

    board = collect(BOOKS, previous, now=NOW, fetch=fetch)
    assert board.due_session == date(2026, 10, 14)
    assert board.problems == ["PROD: DATA 10/13, 1 BEHIND", "LAB: API UNREACHABLE"]
    assert board.books[1].accounts[0].label == "ALPHA V1"  # last good rows kept

    board = collect(BOOKS, {}, now=NOW, fetch=lambda url: status(runner="failed"))
    assert board.problems == ["PROD: RUNNER FAILED", "LAB: RUNNER FAILED"]


def test_session_not_due_before_runner_window():
    early = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)  # 2h after close
    board = collect(BOOKS, {}, now=early, fetch=lambda url: status(last="2026-10-13"))
    assert board.due_session == date(2026, 10, 13) and board.problems == []


def test_run_paper_once_pushes_only_on_change():
    shown = []
    board, outcome = run_paper_once({}, None, now=NOW, books=BOOKS, show=shown.append, fetch=lambda url: status())
    assert outcome.pushed and len(shown) == 1
    board, outcome = run_paper_once({}, board.fingerprint(), now=NOW, books=BOOKS, show=shown.append,
                                    fetch=lambda url: status())
    assert not outcome.pushed and len(shown) == 1
    board, outcome = run_paper_once({}, board.fingerprint(), now=NOW, books=BOOKS, show=shown.append,
                                    fetch=lambda url: status(runner="failed"))
    assert outcome.pushed and outcome.problems and len(shown) == 2


def test_book_json_round_trip():
    book = parse_book("PROD", status(halted=True))
    assert PaperBook.from_json(book.to_json()) == book


def test_render_normal_and_alert():
    ok = collect(BOOKS, {}, now=NOW, fetch=lambda url: status())
    bad = collect(BOOKS, {}, now=NOW, fetch=lambda url: status(halted=True, runner="failed"))
    for board, header_pixel in ((ok, 255), (bad, 0)):
        image = Image.open(io.BytesIO(render_paper_png(board, tz=TZ)))
        assert image.size == (400, 300) and image.mode == "1"
        assert image.getpixel((200, 2)) == header_pixel


def test_paper_tick_refuses_to_share_the_ibkr_page(tmp_path, capsys, monkeypatch):
    from ibkr_zectrix_dashboard.cli import main

    for key, value in (("ZECTRIX_API_KEY", "k"), ("ZECTRIX_PAGE_ID", "2"), ("PAPER_PAGE_ID", "2")):
        monkeypatch.setenv(key, value)
    env_file = tmp_path / "env"
    env_file.write_text("")
    env_file.chmod(0o600)
    assert main(["--env-file", str(env_file), "paper-tick", "--dry-run"]) == 2
    assert "PAPER_PAGE_ID" in capsys.readouterr().err


def test_labels_come_from_configuration():
    labels = parse_labels("alpha_v1=Alpha One, broken, =x")
    assert labels == {"alpha_v1": "ALPHA ONE"}
    assert parse_book("PROD", status(), labels).accounts[0].label == "ALPHA ONE"
    assert parse_book("PROD", status()).accounts[0].label == "ALPHA V1"
