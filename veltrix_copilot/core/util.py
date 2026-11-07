"""Small helpers shared by several modules."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")            # split into sentences / lines
_MARKERS = re.compile(r"^\s*(?:#+|[-*•]|\d+[.)])\s*")   # leading Markdown heading / bullet / number


def clean_line(text: str) -> str:
    return _MARKERS.sub("", text).replace("**", "").strip(" |")


def duration(minutes: float) -> str:
    """184 -> '3h 04m', 45 -> '45m'."""
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def to_utc(local: str, tz: str) -> datetime:
    """A local 'YYYY-MM-DDTHH:MM' in time zone `tz` (UTC if empty) as a UTC datetime."""
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz) if tz else timezone.utc).astimezone(timezone.utc)
