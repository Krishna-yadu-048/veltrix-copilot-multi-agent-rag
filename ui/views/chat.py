"""Chat page: the conversation in the middle, your chats in the sidebar, and a workspace panel on the right
with the latest trip, draft and event."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from ui.components.api import API_URL, api, api_get, when
from ui.components.markdown import md
from ui.components.nav import PAGES

SUGGESTIONS = {
    "Ask the knowledge base": ("menu_book", [
        "What is the hotel cap in Singapore?",
        "Who is the site lead for the Pune office?",
        "Summarize project Kestrel.",
    ]),
    "Plan business travel": ("flight_takeoff", [
        "Plan a 3-day trip from Kochi to Berlin next month with a budget of $2500.",
        "Create a draft email to my manager with the travel plan.",
        "Save this plan to my calendar.",
    ]),
}


# ------------------------------------------------------------------ chat sessions
def start_new_chat() -> None:
    """A chat id is created lazily, when the first message is sent (no empty chats in the list)."""
    st.session_state.session_id = None
    st.session_state.history = []
    st.query_params.clear()


def open_chat(sid: str) -> bool:
    msgs = api_get(f"/sessions/{sid}/messages")
    if msgs is None:
        return False
    st.session_state.session_id = sid
    st.session_state.history = msgs
    st.query_params["chat"] = sid  # a page refresh re-opens this chat
    return True


def ensure_session() -> str | None:
    if st.session_state.get("session_id"):
        return st.session_state.session_id
    data, _ = api("POST", "/session")
    if not data:
        return None
    st.session_state.session_id = data["session_id"]
    st.query_params["chat"] = data["session_id"]
    return data["session_id"]


def sidebar() -> None:
    with st.sidebar:
        if st.button("New chat", icon=":material/add:", use_container_width=True, type="primary"):
            start_new_chat()
            st.rerun()
        st.caption("RECENT CHATS")
        sessions = api_get("/sessions") or []
        if not sessions:
            st.caption("No saved chats yet.")
        for s in sessions[:30]:
            current = s["session_id"] == st.session_state.session_id
            if st.button(s["title"], key=f"chat-{s['session_id']}", use_container_width=True, disabled=current,
                         icon=":material/chat_bubble:" if current else None, type="tertiary",
                         help=f"{s['messages']} messages · last used {when(s['updated_at'])}"):
                open_chat(s["session_id"])
                st.rerun()
        if st.session_state.session_id and st.session_state.history:
            st.divider()
            if st.button("Delete this chat", icon=":material/delete:", use_container_width=True):
                api("DELETE", f"/sessions/{st.session_state.session_id}")
                start_new_chat()
                st.rerun()


# ------------------------------------------------------------------ answers
def _plain(text: str) -> str:
    """Passage text shown as plain lines: no headings/lists from the document's own Markdown."""
    return md(text.replace("[", "(").replace("]", ")").replace("#", "").replace("*", ""))


def render_source(src: dict) -> None:
    """Document, page/section, the sentence the answer used (highlighted), and the full passage."""
    where = [f"page {src['page']}" if src.get("page") else "", f"“{src['section']}”" if src.get("section") else ""]
    st.markdown(md(f":material/description: **{src['doc_name']}**" + "".join(f" · {w}" for w in where if w)))
    quote = (src.get("quote") or "").strip()
    if quote:
        st.markdown(f"> :blue-background[{_plain(quote)}]")
    else:
        st.caption(md(src.get("snippet", "")) + "…")
    if passage := src.get("passage") or "":
        with st.popover("Open passage"):
            body, q = _plain(passage), _plain(quote)
            if q and q in body:
                body = body.replace(q, f":blue-background[{q}]", 1)
            st.markdown(body.replace("\n", "  \n"))


def render_turn(turn: dict) -> None:
    # st.markdown escapes HTML by default; never enable unsafe_allow_html for model output.
    st.markdown(md(turn.get("answer", "")))
    if turn.get("sources"):
        with st.expander(f"Sources ({len(turn['sources'])})", icon=":material/library_books:"):
            for src in turn["sources"]:
                render_source(src)
    for a in turn.get("actions") or []:
        st.success(a, icon=":material/task_alt:")
    if turn.get("plan"):
        t = (turn.get("timings") or {}).get("total")
        st.caption(f":material/hub: {' → '.join(turn['plan'])}" + (f" · {t:.1f}s" if t else ""))


# ------------------------------------------------------------------ page parts
def welcome() -> None:
    st.markdown("## How can I help today?")
    st.caption("Answers come from Veltrix's 650 company documents, with sources. Trips use live prices and the "
               "Veltrix travel policy; email drafts go to your manager and are never sent.")
    cols = st.columns(len(SUGGESTIONS), gap="medium")
    for col, (title, (icon, prompts)) in zip(cols, SUGGESTIONS.items()):
        with col.container(border=True):
            st.markdown(f"**:material/{icon}: {title}**")
            for q in prompts:
                if st.button(q, key=f"ex-{q}", use_container_width=True, type="secondary"):
                    st.session_state.pending = q
                    st.rerun()


def workspace() -> None:
    """Right-hand panel: what the assistant has made so far."""
    st.markdown("**Workspace**")
    trips = api_get("/trips", {"limit": 3}) or []
    with st.container(border=True):
        st.markdown(":material/flight_takeoff: **Latest trip**")
        if trips:
            t = trips[0]
            st.markdown(md(f"Trip #{t['id']} · {t['origin']} → {t['destination']}  \n"
                           f"{t['start']} – {t['end']}" + (f" · ${t['total_usd']:,.0f}" if t.get("total_usd") else "")))
            st.caption(t["status"])
        else:
            st.caption("No trips yet.")
        st.page_link(PAGES["trips"], label="All trips", icon=":material/arrow_forward:")
    drafts = api_get("/drafts") or []
    with st.container(border=True):
        st.markdown(":material/outgoing_mail: **Latest draft**")
        if drafts:
            st.markdown(md(drafts[0]["subject"][:60]))
            st.caption(f"to {drafts[0]['to']} · {when(drafts[0]['created_at'])} · not sent")
        else:
            st.caption("No drafts yet.")
        st.page_link(PAGES["outbox"], label="Outbox", icon=":material/arrow_forward:")
    events, _ = api("GET", "/events")
    upcoming = [e for e in (events or []) if e["start"][:10] >= datetime.now().strftime("%Y-%m-%d")]
    with st.container(border=True):
        st.markdown(":material/calendar_month: **Next event**")
        if upcoming:
            e = upcoming[0]
            st.markdown(md(e["title"]))
            st.caption(f"{when(e['start'])} → {when(e['end'])}")
        else:
            st.caption("Nothing upcoming.")
        st.page_link(PAGES["calendar"], label="Calendar", icon=":material/arrow_forward:")


# The workspace panel stays in view while the conversation scrolls (it sticks below the top bar).
STICKY_CSS = """<style>
div[data-testid="stLayoutWrapper"]:has(> .st-key-workspace) {
  position: sticky; top: 4.5rem; align-self: flex-start; z-index: 1; }
@media (max-width: 640px) { div[data-testid="stLayoutWrapper"]:has(> .st-key-workspace) { position: static; } }
</style>"""


def yes_no(turn: dict) -> None:
    """Buttons for the question at the end of the last answer ("Save this trip?")."""
    left, right, _ = st.columns([1, 1, 4])
    if left.button("Yes", icon=":material/check:", type="primary", key=f"yes-{len(st.session_state.history)}",
                   use_container_width=True):
        st.session_state.pending = "Yes"
        st.rerun()
    if right.button("No", icon=":material/close:", key=f"no-{len(st.session_state.history)}", use_container_width=True):
        st.session_state.pending = "No"
        st.rerun()


# ------------------------------------------------------------------ the page
def render() -> None:
    if "history" not in st.session_state:
        st.session_state.history, st.session_state.session_id = [], None
        if (sid := st.query_params.get("chat")) and not open_chat(sid):
            start_new_chat()
    sidebar()
    prompt = st.chat_input("Ask about Veltrix, or plan a trip…") or st.session_state.pop("pending", None)

    st.html(STICKY_CSS)
    main, side = st.columns([3, 1], gap="large")
    with side, st.container(key="workspace"):
        workspace()
    with main:
        if not st.session_state.history and not prompt:
            welcome()
        history = st.session_state.history
        for i, turn in enumerate(history):
            with st.chat_message(turn["role"], avatar=":material/person:" if turn["role"] == "user" else ":material/auto_awesome:"):
                if turn["role"] == "user":
                    st.markdown(turn["text"])
                else:
                    render_turn(turn)
                    if turn.get("ask") == "yes_no" and i == len(history) - 1 and not prompt:
                        yes_no(turn)
        if prompt:
            with st.chat_message("user", avatar=":material/person:"):
                st.markdown(prompt)
            with st.chat_message("assistant", avatar=":material/auto_awesome:"):
                with st.spinner("Working on it… (a small local model can take a little while)"):
                    sid = ensure_session()
                    if sid is None:
                        turn = {"role": "assistant", "answer": f"⚠️ Could not start a chat: is the API running at {API_URL}?"}
                    else:
                        data, err = api("POST", "/chat", {"session_id": sid, "message": prompt})
                        if err and err.startswith("Unknown chat"):   # deleted elsewhere: start a fresh one, retry once
                            st.session_state.session_id = None
                            data, err = api("POST", "/chat", {"session_id": ensure_session(), "message": prompt})
                        turn = {"role": "assistant", **data} if data else {"role": "assistant", "answer": f"⚠️ {err}"}
                render_turn(turn)
            st.session_state.history += [{"role": "user", "text": prompt}, turn]
            st.rerun()      # redraw so the sidebar and workspace show the new chat, trip, draft and events
