"""ZECTRIX Open API client (device discovery and 400x300 image delivery)."""

from __future__ import annotations

import io
import json
import urllib.parse
import uuid
from typing import Any, Callable

from .errors import DashboardError
from .http import request_bytes

ZECTRIX_BASE_URL = "https://cloud.zectrix.com"

Requester = Callable[..., bytes]


def masked(value: str) -> str:
    compact = value.replace(":", "").replace("-", "")
    return "*" * len(compact) if len(compact) <= 4 else f"{'*' * (len(compact) - 4)}{compact[-4:]}"


def _decode(payload: bytes, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DashboardError(f"ZECTRIX returned invalid JSON for {what}") from exc
    if not isinstance(parsed, dict):
        raise DashboardError(f"ZECTRIX returned an unexpected {what} response")
    if parsed.get("code") not in (None, 0):
        raise DashboardError(f"ZECTRIX rejected {what}: code {parsed.get('code')}")
    return parsed


class ZectrixClient:
    def __init__(self, api_key: str, *, base_url: str = ZECTRIX_BASE_URL, request: Requester = request_bytes):
        if not api_key:
            raise DashboardError("ZECTRIX_API_KEY is not configured")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._request = request

    def devices(self) -> list[dict[str, Any]]:
        response = _decode(self._request(f"{self._base}/open/v1/devices", headers={"X-API-Key": self._key}), "device list")
        devices = response.get("data", [])
        if not isinstance(devices, list):
            raise DashboardError("ZECTRIX device list is malformed")
        return [device for device in devices if isinstance(device, dict)]

    def resolve_device(self, configured: str) -> str:
        if configured:
            return configured
        devices = self.devices()
        if len(devices) != 1:
            raise DashboardError(
                "No ZECTRIX device is bound" if not devices else "Several ZECTRIX devices are bound; set ZECTRIX_DEVICE_ID"
            )
        device_id = str(devices[0].get("deviceId") or devices[0].get("id") or "").strip()
        if not device_id:
            raise DashboardError("The bound ZECTRIX device has no ID")
        return device_id

    def push_png(self, png: bytes, *, device_id: str, page_id: str) -> dict[str, Any]:
        """Returns the API's `data` object (currently just pageId) for logging."""
        boundary = f"----ibkr-zectrix-{uuid.uuid4().hex}"
        body = io.BytesIO()
        for name, value in (("dither", "false"), ("pageId", page_id)):
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="images"; filename="dashboard.png"\r\n'
            "Content-Type: image/png\r\n\r\n".encode()
        )
        body.write(png)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        url = f"{self._base}/open/v1/devices/{urllib.parse.quote(device_id, safe='')}/display/image"
        response = self._request(
            url,
            method="POST",
            headers={"X-API-Key": self._key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            data=body.getvalue(),
            attempts=3,
        )
        data = _decode(response, "the image").get("data")
        return data if isinstance(data, dict) else {}
