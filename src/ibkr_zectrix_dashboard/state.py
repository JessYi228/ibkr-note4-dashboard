"""Small private JSON state: last good snapshot, attempts and what the screen shows."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .errors import DashboardError
from .snapshot import Snapshot

STATE_VERSION = 1
FIRST_SEEN_LIMIT = 60


def _ts(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


@dataclass
class State:
    last_good: Snapshot | None = None
    last_good_fetched_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    # report date -> when we first saw it; evidence for tuning REFRESH_* minutes.
    first_seen: dict[str, str] = field(default_factory=dict)
    displayed_fingerprint: str | None = None
    displayed_status: str | None = None
    displayed_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "last_good": self.last_good.to_json() if self.last_good else None,
            "last_good_fetched_at": _ts(self.last_good_fetched_at),
            "last_attempt_at": _ts(self.last_attempt_at),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "first_seen": dict(sorted(self.first_seen.items())[-FIRST_SEEN_LIMIT:]),
            "displayed": {
                "fingerprint": self.displayed_fingerprint,
                "status": self.displayed_status,
                "at": _ts(self.displayed_at),
            },
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "State":
        if payload.get("version") != STATE_VERSION:
            raise DashboardError("Unsupported state file version")
        displayed = payload.get("displayed") or {}
        return cls(
            last_good=Snapshot.from_json(payload["last_good"]) if payload.get("last_good") else None,
            last_good_fetched_at=_parse_ts(payload.get("last_good_fetched_at")),
            last_attempt_at=_parse_ts(payload.get("last_attempt_at")),
            last_error=payload.get("last_error"),
            consecutive_failures=int(payload.get("consecutive_failures") or 0),
            first_seen={str(k): str(v) for k, v in (payload.get("first_seen") or {}).items()},
            displayed_fingerprint=displayed.get("fingerprint"),
            displayed_status=displayed.get("status"),
            displayed_at=_parse_ts(displayed.get("at")),
        )


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"

    def load(self) -> State:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return State()
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardError(f"State file is unreadable: {self.path}") from exc
        return State.from_json(payload)

    def save(self, state: State) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".json.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_json(), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Prevent overlapping timer runs from fetching or pushing twice."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.directory / "lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DashboardError("Another refresh is already running") from exc
            yield
