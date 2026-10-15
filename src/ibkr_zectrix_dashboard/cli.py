"""Command line entry point. No command here can place, modify or cancel orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from .config import DEFAULT_ENV_FILE, ENV_TEMPLATE, Settings, env, load_env_file
from .errors import DashboardError
from .flex import FlexClient, fetch_snapshot
from .paper import PaperBook, collect, parse_books, parse_labels, run_paper_once
from .refresh import run_once
from .render import Banner, render_png
from .render_paper import render_paper_png
from .sessions import expected_session
from .snapshot import Snapshot
from .state import PaperStateStore, StateStore
from .zectrix import ZectrixClient, masked


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _sample() -> Snapshot:
    text = resources.files("ibkr_zectrix_dashboard").joinpath("assets/sample_snapshot.json").read_text(encoding="utf-8")
    return Snapshot.from_json(json.loads(text))


def _settings(args: argparse.Namespace) -> Settings:
    env_file = Path(args.env_file).expanduser()
    load_env_file(env_file)
    # Optional non-secret companion file for the paper page (books, labels).
    load_env_file(env_file.with_name("paper.env"))
    return Settings.from_env()


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.env_file).expanduser()
    if path.exists():
        raise DashboardError(f"{path} already exists; edit it instead")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(path, ENV_TEMPLATE.encode())
    print(f"Wrote template {path} (mode 600). Fill it in with your own editor.")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    settings = _settings(args)
    snapshot = _sample()
    now = datetime.now(timezone.utc)
    if args.stale:
        banner = Banner(status="stale", fetched_at=now, expected_session=snapshot.report_date, sessions_behind=2,
                        reason="Flex statement not available yet (code 1003)", last_try_at=now)
    else:
        banner = Banner(status="ok", fetched_at=now)
    png = render_png(snapshot, banner, tz=settings.timezone, max_positions=settings.max_positions,
                     font_path=env("DASHBOARD_FONT_PATH"))
    _write_private(Path(args.output), png)
    print(f"Sample preview written: {args.output} (fictional data)")
    return 0


def _log_push(result: dict, page_id: str) -> None:
    # The cloud only stores the page; the device shows it after its next sync.
    print(f"ZECTRIX cloud accepted page {result.get('pageId', page_id)}")


def _live_snapshot(settings: Settings) -> Snapshot:
    client = FlexClient(settings.flex_token)
    return fetch_snapshot(client, settings.flex_query_id, settings.flex_daily_query_id)


def cmd_check(args: argparse.Namespace) -> int:
    """Fetch once and render locally. Never pushes and never touches refresh state."""
    settings = _settings(args)
    snapshot = _live_snapshot(settings)
    png = render_png(snapshot, Banner(status="ok", fetched_at=datetime.now(timezone.utc)),
                     tz=settings.timezone, max_positions=settings.max_positions, font_path=env("DASHBOARD_FONT_PATH"))
    _write_private(Path(args.output), png)
    expectation = expected_session(datetime.now(timezone.utc), settings.policy)
    print(f"Report date {snapshot.report_date} (newest due session {expectation.session})")
    print(
        "Recognized: nav=yes "
        f"cash={'yes' if snapshot.cash is not None else 'no'} "
        f"daily_pnl={'yes' if snapshot.daily_pnl is not None else 'no'} "
        f"positions={len(snapshot.positions)} history_points={len(snapshot.nav_history)}"
    )
    print(f"Rendered without pushing: {args.output} - review it before enabling the timer")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    settings = _settings(args)
    missing = settings.missing()
    if missing:
        raise DashboardError(f"Missing settings: {', '.join(missing)} in {args.env_file}")
    store = StateStore(settings.state_dir)
    image_path = settings.state_dir / "last-display.png"

    def show(snapshot: Snapshot, banner: Banner) -> None:
        png = render_png(snapshot, banner, tz=settings.timezone, max_positions=settings.max_positions,
                         font_path=env("DASHBOARD_FONT_PATH"))
        _write_private(image_path, png)
        if not args.dry_run:
            client = ZectrixClient(settings.zectrix_api_key)
            result = client.push_png(png, device_id=client.resolve_device(settings.zectrix_device_id),
                            page_id=settings.zectrix_page_id)
            _log_push(result, settings.zectrix_page_id)

    with store.locked():
        state = store.load()
        try:
            outcome = run_once(state, now=datetime.now(timezone.utc), policy=settings.policy,
                               fetch=lambda: _live_snapshot(settings), show=show, force=args.force)
        finally:
            if not args.dry_run:
                store.save(state)
    pushed = "pushed" if outcome.pushed and not args.dry_run else ("rendered" if outcome.pushed else "no push")
    print(f"{outcome.action}: {outcome.detail}; screen={outcome.status} ({pushed})")
    return 1 if outcome.failed else 0


def _paper_books() -> list[tuple[str, str]]:
    return parse_books(env("PAPER_BOOKS"))


def cmd_paper_preview(args: argparse.Namespace) -> int:
    """Render the paper page from the local dashboards. Never pushes or saves state."""
    settings = _settings(args)
    board = collect(_paper_books(), {}, now=datetime.now(timezone.utc), labels=parse_labels(env("PAPER_LABELS")))
    _write_private(Path(args.output), render_paper_png(board, tz=settings.timezone, font_path=env("DASHBOARD_FONT_PATH")))
    print(f"Paper preview written: {args.output}; data {board.data_session}, due {board.due_session}")
    for problem in board.problems:
        print(f"problem: {problem}")
    return 0


def cmd_paper_tick(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if not settings.zectrix_api_key:
        raise DashboardError(f"Missing settings: ZECTRIX_API_KEY in {args.env_file}")
    page_id = env("PAPER_PAGE_ID", "2") or "2"
    if page_id == settings.zectrix_page_id:
        raise DashboardError("PAPER_PAGE_ID must differ from ZECTRIX_PAGE_ID so the IBKR page is not overwritten")
    store = PaperStateStore(settings.state_dir)
    image_path = settings.state_dir / "last-paper-display.png"

    def show(board) -> None:
        png = render_paper_png(board, tz=settings.timezone, font_path=env("DASHBOARD_FONT_PATH"))
        _write_private(image_path, png)
        if not args.dry_run:
            client = ZectrixClient(settings.zectrix_api_key)
            _log_push(client.push_png(png, device_id=client.resolve_device(settings.zectrix_device_id),
                                      page_id=page_id), page_id)

    with StateStore(settings.state_dir).locked("paper.lock"):
        raw_books, displayed, displayed_at = store.load()
        previous = {key: PaperBook.from_json(value) for key, value in raw_books.items()}
        now = datetime.now(timezone.utc)
        board, outcome = run_paper_once(previous, displayed, now=now, books=_paper_books(), show=show,
                                        force=args.force, labels=parse_labels(env("PAPER_LABELS")))
        if not args.dry_run:
            fingerprint = board.fingerprint() if outcome.pushed else displayed
            store.save({book.key: book.to_json() for book in board.books}, fingerprint,
                       now.isoformat(timespec="seconds") if outcome.pushed else displayed_at)
    pushed = ("pushed" if not args.dry_run else "rendered") if outcome.pushed else "no push"
    problems = "; ".join(outcome.problems) or "none"
    print(f"paper: {outcome.detail}; problems: {problems} ({pushed})")
    return 1 if outcome.problems else 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    state = StateStore(settings.state_dir).load()
    expectation = expected_session(datetime.now(timezone.utc), settings.policy)
    print(f"Newest due session: {expectation.session} (stale warning after {expectation.stale_at:%Y-%m-%d %H:%M}Z)")
    print(f"Have report date:   {state.last_good.report_date if state.last_good else 'none'}")
    print(f"Fetched at:         {state.last_good_fetched_at or 'never'}")
    print(f"Last attempt:       {state.last_attempt_at or 'never'}")
    print(f"Last error:         {state.last_error or '-'} (consecutive failures {state.consecutive_failures})")
    print(f"Screen shows:       {state.displayed_status or 'nothing pushed yet'} since {state.displayed_at or '-'}")
    for day, seen in list(state.first_seen.items())[-5:]:
        print(f"First seen report {day} at {seen}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    settings = _settings(args)
    devices = ZectrixClient(settings.zectrix_api_key).devices()
    print(f"ZECTRIX devices: {len(devices)}")
    for device in devices:
        device_id = str(device.get("deviceId") or device.get("id") or "")
        print(f"- alias={device.get('alias') or device.get('name') or 'unnamed'!r} id={masked(device_id)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ibkr-zectrix", description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="private KEY=VALUE settings file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="write a settings template (mode 600)").set_defaults(func=cmd_init)
    preview = sub.add_parser("preview", help="render fictional sample data")
    preview.add_argument("--output", default="output/preview.png")
    preview.add_argument("--stale", action="store_true", help="show the STALE banner variant")
    preview.set_defaults(func=cmd_preview)
    check = sub.add_parser("check", help="fetch IBKR once and render locally without pushing")
    check.add_argument("--output", default="output/check.png")
    check.set_defaults(func=cmd_check)
    tick = sub.add_parser("tick", help="scheduled refresh step (run every ~10 minutes)")
    tick.add_argument("--dry-run", action="store_true", help="render only; no push, state not saved")
    tick.add_argument("--force", action="store_true", help="fetch and push even if nothing changed")
    tick.set_defaults(func=cmd_tick)
    paper_preview = sub.add_parser("paper-preview", help="render the paper-account page from local dashboards")
    paper_preview.add_argument("--output", default="output/paper.png")
    paper_preview.set_defaults(func=cmd_paper_preview)
    paper_tick = sub.add_parser("paper-tick", help="scheduled paper-page step (push to PAPER_PAGE_ID on change)")
    paper_tick.add_argument("--dry-run", action="store_true", help="render only; no push, state not saved")
    paper_tick.add_argument("--force", action="store_true", help="push even if nothing changed")
    paper_tick.set_defaults(func=cmd_paper_tick)
    sub.add_parser("status", help="show refresh state without portfolio values").set_defaults(func=cmd_status)
    sub.add_parser("devices", help="list ZECTRIX devices with masked IDs").set_defaults(func=cmd_devices)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
