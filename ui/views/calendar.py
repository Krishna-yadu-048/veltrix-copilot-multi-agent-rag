"""Calendar page: the assistant's events as an agenda, and your Google Calendar embedded."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from ui.components.api import api, api_get


def _agenda(events: list[dict]) -> None:
    if not events:
        st.caption("No events yet. Plan a trip, then ask: *Save this plan to my calendar.*")
    last_day = None
    for e in sorted(events, key=lambda x: x["start"]):
        day = e["start"][:10]
        if day != last_day:
            st.markdown(f"**{datetime.fromisoformat(day).strftime('%A %d %B %Y')}**")
            last_day = day
        link = f" · [open in Google]({e['html_link']})" if str(e.get("html_link") or "").startswith("https://") else ""
        end = e["end"][11:16] if e["end"][:10] == day else e["end"].replace("T", " ")
        s_tz, e_tz = e.get("start_tz") or "", e.get("end_tz") or e.get("start_tz") or ""
        if s_tz and e_tz and s_tz != e_tz:  # e.g. a flight: departure and arrival in different zones
            span = f"{e['start'][11:16]} ({s_tz}) → {end} ({e_tz})"
        else:
            span = f"{e['start'][11:16]}–{end}" + (f" ({s_tz})" if s_tz else "")
        st.markdown(f"- {span} · {e['title']}{link}")
        if e.get("notes"):
            st.caption(e["notes"])


def render() -> None:
    info = api_get("/calendar/info") or {}
    events, error = api("GET", "/events")
    st.title(":material/calendar_month: Calendar")
    st.caption(f"Google Calendar · calendar `{info.get('calendar_id', 'primary')}` · "
               f"times in {info.get('timezone', '')} unless shown otherwise")
    if info and not info.get("connected"):
        st.error(info.get("error") or "Google Calendar is not connected.")
        st.code("uv run scripts/google_auth.py", language="bash")
    if info.get("open_url"):
        st.link_button("Open Google Calendar ↗", info["open_url"])
    if error:
        st.error(error)
    events = events or []
    st.write(f"{len(events)} event(s) added by the assistant.")

    agenda, google = st.tabs(["Agenda", "Google Calendar"])
    with agenda:
        _agenda(events)
    with google:
        if embed := info.get("embed_url"):
            st.caption("This is Google's own calendar view. It shows your events only if this browser is "
                       "signed in to the same Google account; if it stays blank, use 'Open Google Calendar'.")
            st.iframe(embed, height=640)
        else:
            st.info("The embedded view appears after the assistant has added its first event, or set "
                    "GOOGLE_CALENDAR_ID in .env to a specific calendar ID.")
