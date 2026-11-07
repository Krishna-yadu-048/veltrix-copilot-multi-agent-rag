"""Status page: which services are running, the current settings, the theme, and a full reset."""
from __future__ import annotations

import streamlit as st

from ui.components import theme
from ui.components.api import API_URL, api_get, api_post
from ui.components.markdown import md

SERVICES = [("database", "Database", "Postgres + pgvector"), ("ollama", "Model", "Ollama"),
            ("mcp_travel", "Travel", "Travel tools (MCP)"), ("mcp_email", "Email", "Email tools (MCP)"),
            ("mcp_calendar", "Calendar", "Calendar tools (MCP)")]
BACKENDS = {"google": "Google Calendar", "serpapi": "SerpAPI (live)"}


def dot(ok: bool | None) -> str:
    return "🟢" if ok else ("⚪" if ok is None else "🔴")


def _kv(rows: list[tuple[str, object]]) -> None:
    """Label / value pairs, one per line: grey label, value after it."""
    st.markdown(md("  \n".join(f":gray[{k}] &nbsp;·&nbsp; {v if v not in (None, '') else '—'}" for k, v in rows)))


def _yes(v: bool) -> str:
    return "on" if v else "off"


def status_tab(health: dict | None, api_url: str) -> None:
    if health is None:
        st.error(f"The API isn't reachable at {api_url}. Start everything with `uv run scripts/dev.py`.", icon="🔴")
        return
    ok_count = sum(bool(health.get(k, {}).get("ok")) for k, _, _ in SERVICES)
    st.markdown(f"**{ok_count} of {len(SERVICES)} services running** · API at `{api_url}`")
    cols = st.columns(2)
    for i, (key, short, label) in enumerate(SERVICES):
        info = health.get(key, {})
        with cols[i % 2].container(border=True):
            backend = f" · :blue-badge[{BACKENDS.get(info.get('backend'), info.get('backend'))}]" if info.get("backend") else ""
            st.markdown(f"{dot(info.get('ok'))} **{label}**{backend}")
            details = []
            if key == "database" and info.get("ok"):
                details.append(f"{info['documents']} documents · {info['chunks']} passages")
            if key == "ollama":
                details.append(f"chat model {'✓' if info.get('chat_model_pulled') else '✗ not pulled'} · "
                               f"embedding model {'✓' if info.get('embed_model_pulled') else '✗ not pulled'}")
                for m in info.get("loaded", []):
                    details.append(f"{m['model']}: {m['gpu_percent']}% on GPU" +
                                   ("" if m["gpu_percent"] == 100 else " ⚠️ spilled to CPU"))
            if key.startswith("mcp_") and info.get("ok"):
                details.append(f"{len(info.get('tools') or [])} tools: {', '.join(info.get('tools') or [])}")
            if not info.get("ok") and info.get("error"):
                details.append(f"⚠️ {info['error']}")
            if info.get("warning"):
                details.append(f"⚠️ {info['warning']}")
            st.caption(md("  \n".join(details) or "—"))
    if health.get("mcp_travel", {}).get("ok"):
        t = api_get("/travel/info") or {}
        left = t.get("searches_left_at_serpapi")
        st.caption(md(f"SerpAPI live searches this month: {t.get('live_searches', '?')} / {t.get('monthly_limit', '?')} "
                      f"(safety cap){'' if left is None else f' · {left} left on your plan'}"
                      + ("" if t.get("key_set") else " · ⚠️ no SERPAPI_API_KEY: trip planning won't work")))
    if st.button("Refresh", icon="🔄", key="settings-refresh"):
        st.rerun()


def system_tab() -> None:
    info = api_get("/system")
    if not info:
        st.warning("System details are unavailable while the API is down.")
        return
    a, m, r, ag, u, sv = (info[k] for k in ("api", "model", "rag", "agents", "user", "services"))
    left, right = st.columns(2)
    with left.container(border=True):
        st.markdown("**🧠 Model**")
        _kv([("Chat model", f"`{m['chat_model']}`"), ("Embeddings", f"`{m['embed_model']}` ({m['embed_dim']}-d)"),
             ("Context", f"{m['context_tokens']:,} tokens"), ("Embeddings on CPU", _yes(m["embeddings_on_cpu"])),
             ("Reasoning text", "off" if m["thinking_disabled"] else "allowed"), ("Ollama", f"`{m['ollama_url']}`"),
             ("Kept loaded", m["keep_alive"])])
    with right.container(border=True):
        st.markdown("**🔌 API**")
        _kv([("Address", f"`{a['url']}`"), ("Version", a["version"]), ("Python", a["python"]),
             ("Request timeout", f"{a['request_timeout_s']:.0f} s"), ("API docs", f"[{a['url']}/docs]({a['url']}/docs)")])
    left, right = st.columns(2)
    with left.container(border=True):
        st.markdown("**📚 Document search (RAG)**")
        _kv([("Passages per answer", r["top_k"]), ("Relevance gate", f"distance ≤ {r['relevance_gate']}"),
             ("Passage size", f"{r['chunk_size']} chars, {r['chunk_overlap']} overlap"),
             ("Max context", f"{r['max_context_chars']:,} chars")])
    with right.container(border=True):
        st.markdown("**🤖 Agents and you**")
        _kv([("Model settles unclear follow-ups", _yes(ag["llm_followups"])),
             ("Home city", u["home_city"]), ("Manager (drafts go to)", u["manager_email"]), ("Your name", u["name"])])
    with st.container(border=True):
        st.markdown("**🗄️ Services**")
        _kv([("Database", f"`{sv['database']}`"), ("Travel tools", f"`{sv['travel']}`"),
             ("Email tools", f"`{sv['email']}`"), ("Calendar tools", f"`{sv['calendar']}`")])
    st.caption("Change these in `.env` and restart. Keys and passwords are never shown here.")


def reset_tab() -> None:
    chats = api_get("/sessions", {"limit": 200}) or []
    drafts = api_get("/drafts") or []
    trip_rows = api_get("/trips", {"limit": 200}) or []
    events = api_get("/events") or []
    with st.container(border=True):
        st.markdown("#### 🧨 Full reset")
        st.markdown(md(f"Permanently deletes **{len(chats)} chat{'s' * (len(chats) != 1)}**, "
                       f"**{len(drafts)} email draft{'s' * (len(drafts) != 1)}**, "
                       f"**{len(trip_rows)} trip{'s' * (len(trip_rows) != 1)}** (with their history) and "
                       f"**{len(events)} calendar event{'s' * (len(events) != 1)}** the assistant added."))
        st.caption("Company documents are kept. With Google Calendar, only events the assistant created are removed; "
                   "your own events are never touched. This can't be undone.")
        typed = st.text_input("Type **CONFIRM** to continue", key="reset-confirm", placeholder="CONFIRM")
        if st.button("Delete everything", type="primary", icon="🗑️", use_container_width=True,
                     disabled=typed != "CONFIRM", key="reset-go"):
            with st.spinner("Deleting…"):
                data, err = api_post("/admin/reset", {"confirm": typed})
            if err:
                st.error(err)
                return
            def n(key: str, word: str) -> str:
                k = int(data.get(key) or 0)
                return f"{k} {word}{'' if k == 1 else 's'}"
            done = (f"Deleted {n('chats', 'chat')}, {n('email_drafts', 'draft')}, {n('trips', 'trip')} and "
                    f"{n('calendar_events', 'calendar event')}. Documents were kept.")
            st.session_state.reset_msg = (done, data.get("errors") or [])
            st.session_state.pop("reset-confirm", None)
            st.session_state.history, st.session_state.session_id = [], None   # the open chat is gone too
            st.query_params.clear()
            st.rerun()


def render(health: dict | None) -> None:
    head = st.columns([3, 1])
    head[0].title(":material/monitor_heart: Status")
    theme.switcher(head[1])
    if msg := st.session_state.pop("reset_msg", None):
        st.success(msg[0], icon=":material/check:")
        for err in msg[1]:
            st.warning(err)
    status, system, reset = st.tabs(["Services", "System", "Reset"])
    with status:
        status_tab(health, API_URL)
    with system:
        system_tab()
    with reset:
        reset_tab()
