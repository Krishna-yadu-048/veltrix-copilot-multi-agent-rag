"""The web app's only link to the backend: small helpers around the Veltrix Copilot API."""
from __future__ import annotations

import os
from datetime import datetime

import httpx

from veltrix_copilot.core.config import settings

API_URL = os.environ.get("API_URL", settings.api_url)
# A multi-agent request on a 4 GB GPU can take a minute or more: never use httpx's 5 s default.
TIMEOUT = httpx.Timeout(settings.request_timeout_seconds + 10, connect=5)


def api(method: str, path: str, body: dict | None = None, params: dict | None = None,
        content: bytes | None = None) -> tuple[object | None, str | None]:
    """Call the API; returns (data, None) or (None, the API's error message)."""
    try:
        r = httpx.request(method, f"{API_URL}{path}", json=body, params=params, content=content, timeout=TIMEOUT)
    except httpx.TimeoutException:
        return None, "The request timed out. Check `ollama ps` to see if the model spilled to CPU."
    except httpx.HTTPError as e:
        return None, f"API not reachable at {API_URL}: {e}"
    if r.status_code == 200:
        return r.json(), None
    try:
        detail = r.json().get("detail", r.text)
        return None, detail if isinstance(detail, str) else "; ".join(d.get("msg", str(d)) for d in detail)
    except ValueError:
        return None, r.text


def api_get(path: str, params: dict | None = None):
    return api("GET", path, params=params)[0]


def api_post(path: str, body: dict) -> tuple[object | None, str | None]:
    return api("POST", path, body)


def when(iso: str) -> str:
    """'07 Dec 14:05' from an ISO date-time."""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %H:%M")
    except ValueError:
        return iso
