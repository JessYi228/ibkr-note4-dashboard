"""Read-only summary of the local paper-broker books for a separate display page.

Only the loopback dashboards' `/api/status` endpoints are read; ledgers,
runners and configs are never touched. Benchmarks (SPY/QQQ) are shown in each
book's section bar; every other account gets a row.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .config import RefreshPolicy, env
from .errors import DashboardError
from .sessions import expected_session, sessions_between
from .snapshot import optional_float

BENCHMARKS = {"benchmark_spy": "SPY", "benchmark_qqq": "QQQ"}
HEALTHY_RUNNER = {"ok", "waiting"}

GetJson = Callable[[str], Any]


def parse_labels(spec: str) -> dict[str, str]:
    """`account_id=SHORT NAME,...` from PAPER_LABELS; unlisted accounts show their ID."""
    labels = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        account_id, _, label = item.partition("=")
        if account_id.strip() and label.strip():
            labels[account_id.strip()] = label.strip().upper()[:14]
    return labels


def label_for(account_id: str, labels: dict[str, str]) -> str:
    return labels.get(account_id) or account_id.replace("_", " ").upper()[:14]


def parse_books(spec: str) -> list[tuple[str, str]]:
    books = []
    for item in filter(None, (part.strip() for part in spec.split(","))):
        key, _, url = item.partition("=")
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise DashboardError(f"PAPER_BOOKS entry {key or item!r} must be a loopback http URL")
        books.append((key.strip().upper()[:6], url.strip().rstrip("/")))
    if not books:
        raise DashboardError("PAPER_BOOKS is not set (e.g. MAIN=http://127.0.0.1:8000)")
    return books


def get_json(url: str) -> Any:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise DashboardError(f"{urllib.parse.urlsplit(url).netloc} unreachable") from exc


@dataclass
class PaperAccount:
    account_id: str
    label: str
    equity: float
    initial: float
    halted: bool
    closes: list[float]
    day_pct: float | None

    @property
    def total_pct(self) -> float:
        return (self.equity / self.initial - 1) * 100 if self.initial else 0.0


@dataclass
class PaperBook:
    key: str
    start: date | None
    last_session: date | None
    accounts: list[PaperAccount] = field(default_factory=list)
    benchmarks: dict[str, float] = field(default_factory=dict)  # label -> total %
    problem: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat() if self.start else None
        payload["last_session"] = self.last_session.isoformat() if self.last_session else None
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PaperBook":
        return cls(
            key=payload["key"],
            start=date.fromisoformat(payload["start"]) if payload.get("start") else None,
            last_session=date.fromisoformat(payload["last_session"]) if payload.get("last_session") else None,
            accounts=[PaperAccount(**item) for item in payload.get("accounts", [])],
            benchmarks=dict(payload.get("benchmarks", {})),
            problem=payload.get("problem"),
        )


def _closes(rows: list[dict[str, Any]]) -> tuple[list[float], date | None]:
    """One equity value per session: the last mark of each session (the close)."""
    by_session: dict[str, float] = {}
    for row in sorted(rows, key=lambda r: str(r.get("timestamp", ""))):
        value = optional_float(row.get("equity"))
        if value is not None and row.get("session"):
            by_session[str(row["session"])] = value
    first = min(by_session) if by_session else None
    return [by_session[key] for key in sorted(by_session)], date.fromisoformat(first) if first else None


def parse_book(key: str, status: dict[str, Any], labels: dict[str, str] | None = None) -> PaperBook:
    if status.get("mode") != "PAPER_ONLY":
        raise DashboardError(f"{key}: refusing a book that is not PAPER_ONLY")
    health = status.get("health") or {}
    accounts: list[PaperAccount] = []
    benchmarks: dict[str, float] = {}
    starts: list[date] = []
    for raw in status.get("accounts", []):
        account_id = str(raw.get("account_id"))
        equity = optional_float(raw.get("equity"))
        initial = optional_float(raw.get("initial_cash"))
        if equity is None or not initial:
            continue
        closes, first = _closes((status.get("equity") or {}).get(account_id, []))
        if first:
            starts.append(first)
        day_pct = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 and closes[-2] else None
        account = PaperAccount(account_id, label_for(account_id, labels or {}), equity, initial, bool(raw.get("halted")),
                               closes[-60:], day_pct)
        if account_id in BENCHMARKS:
            benchmarks[BENCHMARKS[account_id]] = account.total_pct
        else:
            accounts.append(account)
    last = health.get("last_session")
    book = PaperBook(key, min(starts) if starts else None, date.fromisoformat(last) if last else None,
                     accounts, benchmarks)
    runner = str(health.get("runner_status") or health.get("status") or "unknown")
    if health.get("heartbeat_stale"):
        book.problem = "RUNNER HEARTBEAT STALE"
    elif runner not in HEALTHY_RUNNER:
        book.problem = f"RUNNER {runner.upper()}"
    elif int(health.get("backlog_count") or 0):
        book.problem = f"BACKLOG {health.get('backlog_count')}"
    return book


@dataclass
class PaperBoard:
    books: list[PaperBook]
    fetched_at: datetime
    due_session: date

    @property
    def data_session(self) -> date | None:
        sessions = [book.last_session for book in self.books if book.last_session]
        return min(sessions) if sessions else None

    @property
    def problems(self) -> list[str]:
        return [f"{book.key}: {book.problem}" for book in self.books if book.problem]

    def fingerprint(self) -> str:
        payload = {"books": [book.to_json() for book in self.books], "due": self.due_session.isoformat()}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def paper_policy() -> RefreshPolicy:
    """Runners commit ~1.5h after the close; flag a book once it is this late."""
    try:
        minutes = int(env("PAPER_STALE_AFTER_MINUTES", "240"))
    except ValueError as exc:
        raise DashboardError("PAPER_STALE_AFTER_MINUTES must be an integer") from exc
    delay = timedelta(minutes=minutes)
    return RefreshPolicy(ready_after=delay, stale_after=delay)


def collect(books: list[tuple[str, str]], previous: dict[str, PaperBook], *, now: datetime,
            fetch: GetJson = get_json, labels: dict[str, str] | None = None) -> PaperBoard:
    """Read every book; an unreachable book keeps its last rows but is flagged."""
    due = expected_session(now, paper_policy()).session
    result = []
    for key, url in books:
        try:
            book = parse_book(key, fetch(f"{url}/api/status"), labels)
        except DashboardError as exc:
            old = previous.get(key)
            book = PaperBook.from_json(old.to_json()) if old else PaperBook(key, None, None)
            book.problem = "API UNREACHABLE" if "unreachable" in str(exc) else str(exc)[:40].upper()
        if book.problem is None and (book.last_session is None or book.last_session < due):
            behind = sessions_between(book.last_session, due) if book.last_session else 0
            book.problem = f"DATA {book.last_session:%m/%d}, {behind} BEHIND" if book.last_session else "NO DATA"
        result.append(book)
    return PaperBoard(result, now, due)


@dataclass
class PaperOutcome:
    pushed: bool
    problems: list[str]
    detail: str


def run_paper_once(previous: dict[str, PaperBook], displayed: str | None, *, now: datetime,
                   books: list[tuple[str, str]], show: Callable[[PaperBoard], None],
                   fetch: GetJson = get_json, force: bool = False,
                   labels: dict[str, str] | None = None) -> tuple[PaperBoard, PaperOutcome]:
    """Collect, and show only when the screen's meaning changed. Caller persists the board."""
    board = collect(books, previous, now=now, fetch=fetch, labels=labels)
    detail = f"data {board.data_session}, due {board.due_session}"
    if board.fingerprint() == displayed and not force:
        return board, PaperOutcome(False, board.problems, detail)
    show(board)
    return board, PaperOutcome(True, board.problems, detail)
