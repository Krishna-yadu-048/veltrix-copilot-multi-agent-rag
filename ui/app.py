"""Veltrix Copilot web app (Streamlit): uv run streamlit run ui/app.py
Top navigation between six pages; every page talks only to the API (ui/components/api.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui.components.api import api_get  # noqa: E402
from ui.components.nav import PAGES  # noqa: E402
from ui.views import calendar, chat, knowledge, outbox, status, trips  # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"

st.set_page_config(page_title="Veltrix Copilot", page_icon=str(ASSETS / "icon.svg"), layout="wide")
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))

health = api_get("/health")
all_ok = bool(health and health.get("ok"))
pages = [
    st.Page(chat.render, title="Chat", icon=":material/forum:", default=True),
    st.Page(trips.render, title="Trips", icon=":material/flight_takeoff:", url_path="trips"),
    st.Page(calendar.render, title="Calendar", icon=":material/calendar_month:", url_path="calendar"),
    st.Page(knowledge.render, title="Knowledge", icon=":material/menu_book:", url_path="knowledge"),
    st.Page(outbox.render, title="Outbox", icon=":material/outgoing_mail:", url_path="outbox"),
    st.Page(lambda: status.render(health), title="Status",
            icon=":material/check_circle:" if all_ok else ":material/error:", url_path="status"),
]
PAGES.update(zip(["chat", "trips", "calendar", "knowledge", "outbox", "status"], pages))
st.navigation(pages, position="top").run()
