"""Google Calendar backend, tested against a fake Calendar API service (no network, no Google account)."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_servers.google_calendar import CREATED_BY, SCOPES, GoogleCalendarBackend, GoogleCalendarError, event_key


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeEvents:
    def __init__(self, store):
        self.store, self.list_calls, self.inserted = store, [], []

    def list(self, **kw):
        self.list_calls.append(kw)

        def run():
            items = self.store
            if "privateExtendedProperty" in kw:
                k, v = kw["privateExtendedProperty"].split("=", 1)
                items = [e for e in items if e["extendedProperties"]["private"].get(k) == v]
            return {"items": items[: kw.get("maxResults", 250)]}
        return _Req(run)

    def insert(self, calendarId, body):
        def run():
            ev = {**body, "id": f"ev{len(self.store) + 1}", "htmlLink": f"https://www.google.com/calendar/event?eid=ev{len(self.store) + 1}",
                  "organizer": {"email": "me@example.com"}}
            self.store.append(ev)
            self.inserted.append(body)
            return ev
        return _Req(run)


class FakeService:
    def __init__(self):
        self._events = FakeEvents([])

    def events(self):
        return self._events


@pytest.fixture
def cal(tmp_path):
    svc = FakeService()
    return GoogleCalendarBackend(tmp_path / "token.json", "primary", "Asia/Kolkata", service=svc), svc


def test_least_privilege_scope():
    assert SCOPES == ["https://www.googleapis.com/auth/calendar.events"]


def test_create_event_body_has_time_zones_and_private_tags(cal):
    backend, svc = cal
    res = backend.create_event("Flight AI317: Kochi → London", "2026-11-02T02:35", "2026-11-02T09:35",
                               "Air India", "Asia/Kolkata", "Europe/London")
    body = svc.events().inserted[0]
    assert body["start"] == {"dateTime": "2026-11-02T02:35:00", "timeZone": "Asia/Kolkata"}
    assert body["end"] == {"dateTime": "2026-11-02T09:35:00", "timeZone": "Europe/London"}
    assert body["extendedProperties"]["private"] == {
        "assistantKey": event_key("Flight AI317: Kochi → London", "2026-11-02T02:35"), "createdBy": CREATED_BY}
    assert res["duplicate"] is False and res["html_link"].startswith("https://") and res["source"] == "google"


def test_saving_the_same_event_twice_does_not_duplicate(cal):
    backend, svc = cal
    a = backend.create_event("Hotel", "2026-11-02T15:00", "2026-11-03T11:00")
    b = backend.create_event("Hotel", "2026-11-02T15:00", "2026-11-03T11:00")
    assert a["duplicate"] is False and b["duplicate"] is True
    assert len(svc.events().inserted) == 1


def test_list_only_reads_events_the_assistant_created(cal):
    backend, svc = cal
    svc.events().store.append({"id": "personal", "summary": "Doctor", "start": {"dateTime": "2026-11-05T10:00:00+05:30"},
                               "end": {"dateTime": "2026-11-05T11:00:00+05:30"}, "extendedProperties": {"private": {}}})
    backend.create_event("Meeting", "2026-11-04T10:00", "2026-11-04T11:00")
    events = backend.list_events("2026-11-01")
    assert [e["title"] for e in events] == ["Meeting"]
    assert svc.events().list_calls[-1]["privateExtendedProperty"] == f"createdBy={CREATED_BY}"


def test_info_derives_embed_url_from_primary_calendar(cal):
    backend, _ = cal
    backend.create_event("Meeting", "2026-11-04T10:00", "2026-11-04T11:00")
    info = backend.info()
    assert info["connected"] is True
    assert info["embed_url"].startswith("https://calendar.google.com/calendar/embed?src=me%40example.com")
    assert "token" not in str(info).lower()


def test_missing_token_gives_setup_instructions(tmp_path):
    backend = GoogleCalendarBackend(tmp_path / "missing.json")
    with pytest.raises(GoogleCalendarError, match="google_auth.py"):
        backend.list_events()
    assert backend.info()["connected"] is False


# ---------------------------------------------------------------- the real server process, Google mode, no token
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_in_google_mode_without_token_explains_how_to_connect(tmp_path, monkeypatch):
    import asyncio

    from veltrix_copilot.core.config import settings
    from veltrix_copilot.tools.mcp_client import ToolError, call_tool

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    env = {**os.environ, "GOOGLE_TOKEN_FILE": str(tmp_path / "none.json"),
           "MCP_DATA_DIR": str(tmp_path), "PYTHONPATH": str(root)}
    proc = subprocess.Popen([sys.executable, "mcp_servers/calendar_server.py", "--port", str(port)], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(75):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        monkeypatch.setattr(settings, "mcp_calendar_url", f"http://127.0.0.1:{port}/mcp")
        info = asyncio.run(call_tool("calendar", "calendar_info", {}))
        assert info["backend"] == "google" and info["connected"] is False
        with pytest.raises(ToolError, match="google_auth.py"):
            asyncio.run(call_tool("calendar", "create_event",
                                  {"title": "x", "start": "2026-11-02T10:00", "end": "2026-11-02T11:00"}))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_missing_google_library_gives_install_instructions(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("googleapiclient"):
            raise ModuleNotFoundError("No module named 'googleapiclient'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("mcp_servers.google_calendar.load_credentials", lambda f: object())
    with pytest.raises(GoogleCalendarError, match="uv sync"):
        GoogleCalendarBackend(tmp_path / "t.json").list_events()


def test_a_new_sign_in_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    import googleapiclient.discovery

    token = tmp_path / "token.json"
    backend = GoogleCalendarBackend(token)
    with pytest.raises(GoogleCalendarError, match=f"No sign-in found at {re.escape(str(token))}"):
        backend.service()
    builds = []
    monkeypatch.setattr("mcp_servers.google_calendar.load_credentials", lambda f: object())
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **kw: builds.append(1) or object())
    token.write_text("{}")                        # scripts/google_auth.py saved a token while the server runs
    first = backend.service()
    assert backend.service() is first and len(builds) == 1          # cached while the file is unchanged
    os.utime(token, (time.time() + 5, time.time() + 5))             # signed in again
    assert backend.service() is not first and len(builds) == 2


def test_env_file_values_ignore_inline_comments(tmp_path, monkeypatch):
    from mcp_servers import common

    (tmp_path / ".env").write_text('GOOGLE_CALENDAR_ID=abc@group.calendar.google.com   # my calendar\n'
                                   'A="quoted # kept"\nB=plain\n', encoding="utf-8")
    monkeypatch.setattr(common, "ROOT", tmp_path)
    for k in ("GOOGLE_CALENDAR_ID", "A", "B"):
        monkeypatch.delenv(k, raising=False)
    assert common.env("GOOGLE_CALENDAR_ID") == "abc@group.calendar.google.com"
    assert common.env("A") == "quoted # kept" and common.env("B") == "plain"
    assert common.env_path("X", Path("secrets/t.json")) == tmp_path / "secrets" / "t.json"
