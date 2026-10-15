import io
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from ibkr_zectrix_dashboard.cli import _sample, main
from ibkr_zectrix_dashboard.config import load_env_file
from ibkr_zectrix_dashboard.errors import DashboardError
from ibkr_zectrix_dashboard.render import Banner, render_png
from ibkr_zectrix_dashboard.zectrix import ZectrixClient

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def image(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_ok_and_stale_render_as_400x300_one_bit():
    snapshot = _sample()
    ok = image(render_png(snapshot, Banner("ok", NOW), tz=TZ))
    stale = image(render_png(snapshot, Banner("stale", NOW, sessions_behind=2, reason="x", last_try_at=NOW), tz=TZ))
    assert ok.size == stale.size == (400, 300)
    assert ok.mode == stale.mode == "1"
    # The stale banner inverts the header band.
    assert ok.getpixel((200, 2)) == 255 and stale.getpixel((200, 2)) == 0


def test_preview_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "missing.env"
    assert main(["--env-file", str(env_file), "preview", "--output", "p.png", "--stale"]) == 0
    assert image((tmp_path / "p.png").read_bytes()).size == (400, 300)


def test_env_file_must_be_private(tmp_path):
    path = tmp_path / "env"
    path.write_text("IBKR_FLEX_TOKEN=x\n")
    os.chmod(path, 0o644)
    with pytest.raises(DashboardError, match="chmod 600"):
        load_env_file(path)


def test_init_refuses_to_overwrite(tmp_path):
    path = tmp_path / "cfg" / "env"
    assert main(["--env-file", str(path), "init"]) == 0
    assert (path.stat().st_mode & 0o777) == 0o600
    assert main(["--env-file", str(path), "init"]) == 2


def test_tick_refuses_without_credentials(tmp_path, capsys):
    path = tmp_path / "env"
    path.write_text("")
    os.chmod(path, 0o600)
    assert main(["--env-file", str(path), "tick"]) == 2
    assert "IBKR_FLEX_TOKEN" in capsys.readouterr().err


def test_zectrix_push_shape():
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return json.dumps({"code": 0, "data": {"pageId": 2, "pushedPages": 1, "totalPages": 2}}).encode()

    client = ZectrixClient("key", request=request)
    assert client.push_png(b"PNG", device_id="AA:BB", page_id="2")["totalPages"] == 2
    url, kwargs = calls[0]
    assert url.endswith("/open/v1/devices/AA%3ABB/display/image")
    assert kwargs["method"] == "POST" and kwargs["headers"]["X-API-Key"] == "key"
    assert b'name="pageId"\r\n\r\n2' in kwargs["data"] and b'name="dither"\r\n\r\nfalse' in kwargs["data"]


def test_change_pct_is_relative_to_value_before_change():
    from ibkr_zectrix_dashboard.drawing import change_pct as _change_pct, signed as _signed

    assert _change_pct(14.02, 2285.63) == pytest.approx(14.02 / (2285.63 - 14.02) * 100)
    assert _change_pct(None, 100.0) is None and _change_pct(5.0, 5.0) is None
    assert (_signed(-1.1), _signed(0.0), _signed(12_345.0), _signed(None)) == ("-1.10", "0.00", "+12.3K", "N/A")


def test_renders_without_daily_query():
    snapshot = _sample()
    snapshot.daily_pnl = None
    for position in snapshot.positions:
        position.daily_pnl = None
    assert image(render_png(snapshot, Banner("ok", NOW), tz=TZ)).size == (400, 300)


def test_window_label_counts_calendar_days_not_trading_days():
    snapshot = _sample()  # 21 trading days from 09/11 to 10/09
    assert len(snapshot.nav_history) == 21 and snapshot.window_label == "29D"
    snapshot.window_days = 30
    assert snapshot.window_label == "30D"
