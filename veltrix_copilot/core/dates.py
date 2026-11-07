"""Date handling in code, not in the LLM (small models get relative dates wrong)."""
from __future__ import annotations

import re
from datetime import date, timedelta

MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1)
}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def next_month_start(today: date) -> date:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return first_monday(year, month)


def parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def resolve_start_date(text: str, today: date, llm_date: str | None = None) -> date:
    """Turn 'next month', 'next week', 'in December', '2026-12-03', 'tomorrow' ... into a date."""
    t = text.lower()

    m = ISO_RE.search(text)
    if m and (d := parse_iso(m.group(1))) and d >= today:
        return d

    if "next month" in t:
        return next_month_start(today)
    if "next week" in t:
        return today + timedelta(days=7 - today.weekday())
    if "tomorrow" in t:
        return today + timedelta(days=1)
    if "today" in t:
        return today

    for name, idx in ((n, i) for i, n in enumerate(WEEKDAYS)):
        if re.search(rf"\b(next |this |on )?{name}\b", t):
            ahead = (idx - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)

    for name, month in MONTHS.items():
        if re.search(rf"\b(in|during|for|on|early|mid|late)\s+{name}\b", t) or re.search(rf"\b{name}\s+\d{{4}}\b", t):
            day_m = re.search(rf"\b{name}\s+(\d{{1,2}})(st|nd|rd|th)?\b", t) or re.search(
                rf"\b(\d{{1,2}})(st|nd|rd|th)?\s+(of\s+)?{name}\b", t)
            year = today.year if month >= today.month else today.year + 1
            if day_m:
                try:
                    d = date(year, month, int(day_m.group(1)))
                    if d < today:
                        d = date(year + 1, month, int(day_m.group(1)))
                    return d
                except ValueError:
                    pass
            d = first_monday(year, month)
            return d if d >= today else first_monday(year + 1, month)

    if (d := parse_iso(llm_date)) and d >= today:
        return d

    two_weeks = today + timedelta(days=14)
    return two_weeks - timedelta(days=two_weeks.weekday())


def has_date_hint(text: str) -> bool:
    """Does the text mention any date at all? (Used to avoid inventing dates for events.)"""
    t = text.lower()
    if ISO_RE.search(text) or re.search(r"\b(today|tomorrow|next week|next month)\b", t):
        return True
    if any(re.search(rf"\b{w}\b", t) for w in WEEKDAYS):
        return True
    return any(re.search(rf"\b{m}\b", t) for m in MONTHS if len(m) > 3 or m in {"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"})


TIME_IN_TEXT_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s?(?:am|pm))\b|\b(\d{1,2}:\d{2})\b", re.I)


def find_time(text: str) -> str | None:
    m = TIME_IN_TEXT_RE.search(text)
    return (m.group(1) or m.group(2)) if m else None


def parse_time(value: str | None, default: str = "09:00") -> str:
    """'3pm' / '15:00' / '9:30 am' -> 'HH:MM'."""
    if not value:
        return default
    v = value.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", v)
    if not m:
        return default
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mins < 60):
        return default
    return f"{h:02d}:{mins:02d}"


def fmt_day(d: date) -> str:
    return d.strftime("%a %d %b %Y")
