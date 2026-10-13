"""Minimal HTTPS client whose errors reveal only status and host, never URLs."""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .errors import DashboardError

USER_AGENT = f"ibkr-zectrix-dashboard/{__version__}"


def require_https(url: str, label: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DashboardError(f"{label} must use HTTPS")


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30,
    attempts: int = 1,
) -> bytes:
    require_https(url, "request URL")
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    context = ssl.create_default_context()
    host = urllib.parse.urlsplit(url).hostname
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise DashboardError(f"HTTP {exc.code} from {host}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt == attempts - 1:
                reason = getattr(exc, "reason", exc)
                raise DashboardError(f"Could not reach {host}: {reason}") from None
        time.sleep(min(2**attempt, 8))
    raise DashboardError(f"Could not reach {host}")
