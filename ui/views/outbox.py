"""Outbox page: the email drafts the assistant saved for your manager. Drafts are never sent."""
from __future__ import annotations

import streamlit as st

from ui.components.api import api, api_get, when


def render() -> None:
    st.title(":material/outgoing_mail: Outbox")
    st.caption("Email drafts to your manager. Nothing is ever sent: copy a draft into your mail app when it's ready.")
    drafts = api_get("/drafts", {"include_body": "true"})
    if drafts is None:
        st.error("The email tool isn't reachable. Start everything with `uv run scripts/dev.py`.")
        return
    if not drafts:
        st.info("No drafts yet. In the chat, try: *Create a draft email to my manager with the travel plan.*",
                icon=":material/drafts:")
        return
    left, right = st.columns([1, 2], gap="large")
    with left:
        labels = {d["draft_id"]: f"{d['subject'][:60] or '(no subject)'} · {when(d['created_at'])}" for d in drafts}
        chosen = st.radio("Drafts", list(labels), format_func=labels.get, label_visibility="collapsed")
    d = next(x for x in drafts if x["draft_id"] == chosen)
    with right.container(border=True):
        st.markdown(f"#### {d['subject'] or '(no subject)'}")
        st.caption(f"To {d['to']} · saved {when(d['created_at'])} · draft, not sent")
        st.code(d.get("body") or "", language=None, wrap_lines=True)
        if st.button("Delete draft", icon=":material/delete:", key=f"del-{d['draft_id']}"):
            api("DELETE", f"/drafts/{d['draft_id']}")
            st.rerun()
