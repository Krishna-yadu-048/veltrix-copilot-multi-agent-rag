"""Calendar MCP server: your Google Calendar (sign in once with scripts/google_auth.py).

Only events the assistant created are listed or deleted; the clash check reads just titles and times."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_servers.common import ROOT, env, env_path, make_server
from mcp_servers.google_calendar import GoogleCalendarBackend, GoogleCalendarError

mcp, PORT = make_server("calendar", 8103)
_google: GoogleCalendarBackend | None = None


def default_tz() -> str:
    return env("TIMEZONE", "Asia/Kolkata")


def google() -> GoogleCalendarBackend:
    global _google
    if _google is None:
        _google = GoogleCalendarBackend(
            token_file=env_path("GOOGLE_TOKEN_FILE", ROOT / "secrets" / "google_token.json"),
            calendar_id=env("GOOGLE_CALENDAR_ID", "primary"),
            default_tz=default_tz(),
        )
    return _google


def _google_call(method: str, *args):
    """Run one Google Calendar call; its errors become tool errors with a readable message."""
    try:
        return getattr(google(), method)(*args)
    except GoogleCalendarError as err:
        raise ValueError(str(err)) from err


def _parse(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{field} must be ISO format YYYY-MM-DDTHH:MM") from e


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or default_tz())
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown time zone '{name}'. Use names like Asia/Kolkata or Europe/London.") from e


@mcp.tool()
def create_event(title: str, start: str, end: str, notes: str = "", start_tz: str = "", end_tz: str = "") -> dict:
    """Create a calendar event. Saving the same event twice (same title and start) doesn't create a copy.

    Args:
        title: event title (max 120 characters)
        start: start time in the start time zone, YYYY-MM-DDTHH:MM
        end: end time in the end time zone, YYYY-MM-DDTHH:MM (must be after start)
        notes: optional notes (max 1000 characters)
        start_tz: IANA time zone of `start`, e.g. "Asia/Kolkata" (default: TIMEZONE setting)
        end_tz: IANA time zone of `end`, e.g. "Europe/London" (default: same as start_tz)
    """
    s, e = _parse(start, "start"), _parse(end, "end")
    # Compare as real instants: a flight can leave at 02:35 in Kochi and land at 08:05 in London.
    if e.replace(tzinfo=_zone(end_tz or start_tz)) <= s.replace(tzinfo=_zone(start_tz)):
        raise ValueError("end must be after start")
    if not title.strip() or len(title) > 120:
        raise ValueError("title must be 1-120 characters")
    return _google_call("create_event", title, f"{s:%Y-%m-%dT%H:%M}", f"{e:%Y-%m-%dT%H:%M}", notes[:1000], start_tz, end_tz)


@mcp.tool()
def list_events(from_date: str = "") -> list[dict]:
    """List the assistant's events sorted by start time, optionally only those on/after from_date (YYYY-MM-DD)."""
    return _google_call("list_events", from_date)


@mcp.tool()
def delete_event(event_id: str) -> dict:
    """Delete an event the assistant created (used when a trip is changed or cancelled).
    Events the assistant didn't create are never touched.

    Args:
        event_id: the event_id returned by create_event
    """
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", event_id or ""):
        raise ValueError("Invalid event id")
    return _google_call("delete_event", event_id)


@mcp.tool()
def find_conflicts(start_utc: str, end_utc: str) -> list[dict]:
    """Events that overlap a time window (used to warn before adding a trip).
    Returns only title, start/end (local, with time zone) and is_leave for each overlapping event.

    Args:
        start_utc: window start, UTC, YYYY-MM-DDTHH:MM
        end_utc: window end, UTC, YYYY-MM-DDTHH:MM
    """
    a = _parse(start_utc, "start_utc").replace(tzinfo=timezone.utc)
    b = _parse(end_utc, "end_utc").replace(tzinfo=timezone.utc)
    if b <= a:
        raise ValueError("end_utc must be after start_utc")
    return _google_call("find_conflicts", a, b)


@mcp.tool()
def calendar_info() -> dict:
    """Whether Google Calendar is connected, and links for viewing it."""
    return google().info()


if __name__ == "__main__":
    print(f"calendar MCP server (Google Calendar) on http://127.0.0.1:{PORT}/mcp")
    mcp.run(transport="streamable-http")
