"""Google Calendar backend for the calendar MCP server."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CREATED_BY = "veltrix-copilot"
NOT_CONNECTED = ("Google Calendar is not connected. Run `uv run scripts/google_auth.py` once "
                 "(see README.md, 'Google Calendar').")
EXPIRED = ("The Google sign-in has expired or was revoked. Run `uv run scripts/google_auth.py` again. "
           "(While the Google Cloud app is in 'Testing' status, sign-ins expire after 7 days.)")


class GoogleCalendarError(RuntimeError):
    pass


def event_key(title: str, start: str) -> str:
    return hashlib.sha1(f"{title.strip()}|{start[:16]}".encode()).hexdigest()[:20]


def _local(dt_str: str) -> str:
    """'2026-11-02T02:35:00+05:30' -> '2026-11-02T02:35' (wall-clock time in the event's zone)."""
    return dt_str[:16] if dt_str else ""


def load_credentials(token_file: Path):
    """Load and, if needed, refresh the saved OAuth token."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GoogleCalendarError("Google auth libraries are not installed in this Python environment. "
                                  "Run: uv sync") from e

    if not token_file.exists():
        raise GoogleCalendarError(NOT_CONNECTED)
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise GoogleCalendarError(EXPIRED) from e
            save_token(token_file, creds.to_json())
        else:
            raise GoogleCalendarError(EXPIRED)
    return creds


def save_token(token_file: Path, token_json: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token_json, encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)  # owner-only on macOS/Linux; no-op on Windows
    except OSError:
        pass


class GoogleCalendarBackend:
    def __init__(self, token_file: Path, calendar_id: str = "primary", default_tz: str = "Asia/Kolkata",
                 service=None):
        self.token_file = token_file
        self.calendar_id = calendar_id or "primary"
        self.default_tz = default_tz
        self._service = service  # injectable for tests
        self._injected = service is not None
        self._stamp: float | None = None

    def _token_stamp(self) -> float | None:
        try:
            return self.token_file.stat().st_mtime
        except OSError:
            return None

    def service(self):
        # A new sign-in (scripts/google_auth.py rewrites the token file) is picked up on the next call,
        # without restarting the server. Tests inject a service and have no token file.
        if self._service is not None and self._injected:
            return self._service
        stamp = self._token_stamp()
        if self._service is None or stamp != self._stamp:
            try:
                from googleapiclient.discovery import build
            except ImportError as e:
                raise GoogleCalendarError(
                    "Google API client library is not installed in this Python environment. "
                    "Run: uv sync") from e

            self._service = None
            if stamp is None:
                raise GoogleCalendarError(f"{NOT_CONNECTED} (No sign-in found at {self.token_file}.)")
            creds = load_credentials(self.token_file)
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            self._stamp = self._token_stamp()     # load_credentials may have refreshed and rewritten it
        return self._service

    def _run(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", "?")
            if status == 401 and not self._injected:
                self._service = None          # build again from the token file on the next call
            if status in (401, 403):
                raise GoogleCalendarError(EXPIRED if status == 401 else
                                          f"Google Calendar refused the request (HTTP {status}). "
                                          "Check that the Calendar API is enabled and GOOGLE_CALENDAR_ID is yours.") from e
            raise GoogleCalendarError(f"Google Calendar error (HTTP {status}).") from e
        except GoogleCalendarError:
            raise
        except Exception as e:
            raise GoogleCalendarError(f"Could not reach Google Calendar ({type(e).__name__}).") from e

    def create_event(self, title: str, start: str, end: str, notes: str = "",
                     start_tz: str = "", end_tz: str = "") -> dict:
        key = event_key(title, start)
        existing = self._run(self.service().events().list(
            calendarId=self.calendar_id, privateExtendedProperty=f"assistantKey={key}",
            maxResults=1, singleEvents=True, showDeleted=False))
        if existing.get("items"):
            return {**self._to_dict(existing["items"][0]), "duplicate": True}

        body = {
            "summary": title.strip(),
            "description": (notes + "\n\n" if notes else "") + "Added by Veltrix Copilot.",
            "start": {"dateTime": f"{start[:16]}:00", "timeZone": start_tz or self.default_tz},
            "end": {"dateTime": f"{end[:16]}:00", "timeZone": end_tz or start_tz or self.default_tz},
            "extendedProperties": {"private": {"assistantKey": key, "createdBy": CREATED_BY}},
        }
        created = self._run(self.service().events().insert(calendarId=self.calendar_id, body=body))
        return {**self._to_dict(created), "duplicate": False}

    def list_events(self, from_date: str = "") -> list[dict]:
        since = (datetime.fromisoformat(from_date[:10]) if from_date
                 else datetime.now() - timedelta(days=60)).replace(tzinfo=timezone.utc)
        resp = self._run(self.service().events().list(
            calendarId=self.calendar_id, privateExtendedProperty=f"createdBy={CREATED_BY}",
            timeMin=since.isoformat(), singleEvents=True, orderBy="startTime", maxResults=250))
        return [self._to_dict(e) for e in resp.get("items", [])]

    def delete_event(self, event_id: str) -> dict:
        """Delete one event, but only if the assistant created it (checked via its private tag)."""
        from googleapiclient.errors import HttpError

        try:
            event = self.service().events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) in (404, 410):
                return {"event_id": event_id, "deleted": False}
            raise GoogleCalendarError(f"Google Calendar error (HTTP {getattr(e.resp, 'status', '?')}).") from e
        private = (event.get("extendedProperties") or {}).get("private") or {}
        if private.get("createdBy") != CREATED_BY:
            raise GoogleCalendarError("That event wasn't created by the assistant, so it won't be deleted.")
        if event.get("status") == "cancelled":
            return {"event_id": event_id, "deleted": False}
        self._run(self.service().events().delete(calendarId=self.calendar_id, eventId=event_id))
        return {"event_id": event_id, "deleted": True}

    def find_conflicts(self, start: datetime, end: datetime) -> list[dict]:
        """Busy events in [start, end) only: title, times, and whether it looks like leave / out of office.
        Events marked 'free' (transparent) are ignored. Nothing is stored."""
        import re

        leave = re.compile(r"\b(leave|vacation|holiday|pto|ooo|out of office|time off|off sick|sick day)\b", re.I)
        resp = self._run(self.service().events().list(
            calendarId=self.calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=50))
        out = []
        for e in resp.get("items", []):
            if e.get("transparency") == "transparent" or e.get("status") == "cancelled":
                continue
            d = self._to_dict(e)
            out.append({"event_id": d["event_id"], "title": d["title"] or "(busy)", "start": d["start"], "end": d["end"],
                        "start_tz": d["start_tz"],
                        "is_leave": e.get("eventType") == "outOfOffice" or bool(leave.search(d["title"] or ""))})
        return out

    def info(self) -> dict:
        """What the UI needs to show / embed the calendar. Never includes the token."""
        out = {"backend": "google", "calendar_id": self.calendar_id, "timezone": self.default_tz,
               "open_url": "https://calendar.google.com/calendar/r", "connected": False, "embed_url": None}
        try:
            embed_id = self.calendar_id
            if embed_id == "primary":  # the primary calendar's real id is the account email
                items = self.list_events()
                embed_id = next((e["organizer"] for e in items if e.get("organizer")), None)
            else:
                self.service()
            out["connected"] = True
            if embed_id:
                out["embed_url"] = (f"https://calendar.google.com/calendar/embed?src={quote(embed_id)}"
                                    f"&ctz={quote(self.default_tz)}&mode=MONTH&showPrint=0")
        except GoogleCalendarError as e:
            out["error"] = str(e)
        return out

    @staticmethod
    def _to_dict(e: dict) -> dict:
        start, end = e.get("start", {}), e.get("end", {})
        return {
            "event_id": e.get("id"),
            "title": e.get("summary", ""),
            "start": _local(start.get("dateTime") or (start.get("date", "") + "T00:00")),
            "end": _local(end.get("dateTime") or (end.get("date", "") + "T00:00")),
            "start_tz": start.get("timeZone", ""),
            "end_tz": end.get("timeZone", ""),
            "notes": (e.get("description") or "").replace("\n\nAdded by Veltrix Copilot.", "")[:1000],
            "html_link": e.get("htmlLink"),
            "organizer": (e.get("organizer") or {}).get("email"),
            "source": "google",
        }


def token_summary(token_file: Path) -> dict:
    """For diagnostics: which scopes the saved token has (never prints the token itself)."""
    if not token_file.exists():
        return {"exists": False}
    data = json.loads(token_file.read_text(encoding="utf-8"))
    return {"exists": True, "scopes": data.get("scopes"), "has_refresh_token": bool(data.get("refresh_token"))}
