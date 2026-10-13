"""One scheduled refresh step.

Run it every ~10 minutes. It only contacts IBKR when the newest NYSE session's
report should exist but we do not have it yet, and only pushes to the display
when what the screen should show has changed. When a report is overdue it
pushes a STALE screen once instead of silently leaving old data up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import RefreshPolicy
from .errors import DashboardError
from .render import Banner
from .sessions import expected_session, sessions_between
from .snapshot import Snapshot
from .state import State

Fetch = Callable[[], Snapshot]
Show = Callable[[Snapshot, Banner], None]


@dataclass(frozen=True)
class Outcome:
    action: str  # up_to_date | waiting | fetched | failed
    pushed: bool
    status: str | None
    detail: str

    @property
    def failed(self) -> bool:
        return self.action == "failed"


def fingerprint(snapshot: Snapshot, banner: Banner) -> str:
    """Hash what the screen means, ignoring timestamps that change every run."""
    payload = {
        "snapshot": snapshot.to_json(),
        "status": banner.status,
        "expected": banner.expected_session.isoformat() if banner.expected_session else None,
        "behind": banner.sessions_behind,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _banner(state: State, expectation, now: datetime) -> Banner:
    snapshot = state.last_good
    assert snapshot is not None
    behind = sessions_between(snapshot.report_date, expectation.session)
    if behind and expectation.is_overdue(now):
        return Banner(
            status="stale",
            fetched_at=state.last_good_fetched_at,
            expected_session=expectation.session,
            sessions_behind=behind,
            reason=state.last_error or "IBKR has not published the report yet",
            last_try_at=state.last_attempt_at,
        )
    return Banner(status="ok", fetched_at=state.last_good_fetched_at)


def run_once(state: State, *, now: datetime, policy: RefreshPolicy, fetch: Fetch, show: Show,
             force: bool = False) -> Outcome:
    """Advance `state` in place; caller persists it."""
    expectation = expected_session(now, policy)
    have = state.last_good.report_date if state.last_good else None
    needs_data = have is None or have < expectation.session
    action, detail = "up_to_date", f"have {have}, expected {expectation.session}"

    if needs_data or force:
        recently_tried = (
            state.last_attempt_at is not None and now - state.last_attempt_at < policy.retry_every
        )
        if recently_tried and not force:
            action, detail = "waiting", f"last attempt {state.last_attempt_at:%H:%M}Z; expected {expectation.session}"
        else:
            state.last_attempt_at = now
            try:
                snapshot = fetch()
            except DashboardError as exc:
                state.last_error = str(exc)[:160]
                state.consecutive_failures += 1
                action, detail = "failed", state.last_error
            else:
                if have is not None and snapshot.report_date < have:
                    state.last_error = f"IBKR returned an older report ({snapshot.report_date})"
                    state.consecutive_failures += 1
                    action, detail = "failed", state.last_error
                else:
                    state.first_seen.setdefault(snapshot.report_date.isoformat(), now.isoformat(timespec="seconds"))
                    state.last_good, state.last_good_fetched_at = snapshot, now
                    state.last_error, state.consecutive_failures = None, 0
                    action, detail = "fetched", f"report {snapshot.report_date}, expected {expectation.session}"
                    if snapshot.report_date < expectation.session:
                        state.last_error = "IBKR has not published the report yet"

    if state.last_good is None:
        return Outcome(action, False, None, detail + "; nothing to display yet")

    banner = _banner(state, expectation, now)
    digest = fingerprint(state.last_good, banner)
    if digest == state.displayed_fingerprint and not force:
        return Outcome(action, False, banner.status, detail)
    show(state.last_good, banner)
    state.displayed_fingerprint, state.displayed_status, state.displayed_at = digest, banner.status, now
    return Outcome(action, True, banner.status, detail)
