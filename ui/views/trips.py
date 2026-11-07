"""The Trips page: see every trip, change one, review the change, then apply or discard it."""
from __future__ import annotations

from datetime import date

import streamlit as st

from ui.components.api import api_get, api_post
from ui.components.markdown import md

BADGE = {"planned": ":green-badge[Planned]", "emailed": ":violet-badge[Emailed]", "in calendar": ":blue-badge[In calendar]",
         "cancelled": ":gray-badge[Cancelled]"}
FILTERS = ["Upcoming", "All", "Past", "Cancelled"]


def badges(status: str) -> str:
    return " ".join(BADGE.get(s.strip(), "") for s in status.split(","))


def _dates(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return f"{s:%a %d %b} – {e:%a %d %b %Y}"


def _keep(key: str, value) -> None:
    st.session_state[key] = value


def _close_dialog() -> None:
    st.session_state.pop("trip_confirm", None)


# Must be answered: Yes applies, No discards. (Closing it with ✕ just goes back to the review.)
@st.dialog("Apply these changes?", on_dismiss=_close_dialog)
def confirm(api_post, prop: dict, calendar: bool, email: bool) -> None:
    res = prop["result"]
    changed = [r for r in res["diff"] if r["changed"]]
    st.write(f"**Trip #{res['trip_id']}** · {len(changed)} change{'s' if len(changed) != 1 else ''}")
    for r in changed:
        st.markdown(md(f"- **{r['field']}:** {r['after']}"))
    steps = []
    if res["new_trip"]:
        steps.append(f"save it as a **new trip** and show Trip #{res['trip_id']} as cancelled")
    steps.append("replace its calendar events" if calendar and res["in_calendar"] else
                 ("add it to your calendar" if calendar else "leave the calendar as it is"))
    if email:
        steps.append("save an *updated plan* email draft (not sent)")
    st.caption("This will " + ", ".join(steps) + ".")
    yes, no = st.columns(2)
    if yes.button("Yes, apply", type="primary", use_container_width=True):
        body = {**prop["change"], "picks": prop["picks"], "base_updated_at": res["base_updated_at"],
                "calendar": calendar, "email": email}
        data, err = api_post(f"/trips/{res['trip_id']}/apply", body)
        if err:
            st.session_state.trip_msg = ("error", err)
        else:
            st.session_state.trip_msg = ("success", f"Saved: {data['label']}.  \n" + "  \n".join(f"✓ {a}" for a in data["actions"]))
            st.session_state.trip_sel = data["trip_id"]
        st.session_state.pop("trip_prop", None)
        _close_dialog()
        st.rerun()
    if no.button("No, discard", use_container_width=True):
        st.session_state.pop("trip_prop", None)
        st.session_state.trip_msg = ("info", "Changes discarded. The trip is unchanged.")
        _close_dialog()
        st.rerun()


def _preview(api_post, trip_id: int, change: dict, picks: dict) -> None:
    data, err = api_post(f"/trips/{trip_id}/preview", {**change, "picks": picks})
    if err:
        st.session_state.trip_msg = ("error", err)
        return
    # A number that never repeats in this browser session: each preview gets fresh widgets (radios, checkboxes).
    n = st.session_state["trip_prop_seq"] = st.session_state.get("trip_prop_seq", 0) + 1
    st.session_state.trip_prop = {"trip": trip_id, "change": change, "picks": data["picks"], "result": data, "n": n}


def edit_form(api_get, api_post, trip: dict) -> None:
    plan, tid = trip["plan"], trip["id"]
    cities = api_get("/cities") or [plan["origin"], plan["destination"]]
    currencies = ["USD", "INR", "GBP", "EUR"]
    st.caption("Change what you need, then preview. Nothing is saved until you apply it.")
    with st.form(f"edit-{tid}", border=True):
        c1, c2 = st.columns(2)
        origin = c1.selectbox("From", cities, index=cities.index(plan["origin"]) if plan["origin"] in cities else 0)
        dest = c2.selectbox("To", cities, index=cities.index(plan["destination"]) if plan["destination"] in cities else 0,
                            help="A different route is saved as a new trip; this one is then shown as cancelled.")
        c3, c4 = st.columns(2)
        start = c3.date_input("Start date", value=max(date.fromisoformat(plan["start"]), date.today()),
                              min_value=date.today(), format="DD/MM/YYYY")
        nights = c4.number_input("Nights", min_value=1, max_value=14, value=int(plan["nights"]), step=1)
        c5, c6 = st.columns([2, 1])
        cur = plan.get("budget_currency") or "USD"
        budget = c5.number_input("Budget (0 = no budget)", min_value=0.0, step=100.0,
                                 value=float(plan.get("budget_amount") or 0) if plan.get("budget") else 0.0)
        currency = c6.selectbox("Currency", currencies, index=currencies.index(cur) if cur in currencies else 0)
        go = st.form_submit_button("Preview changes", type="primary", use_container_width=True, icon="🔍")
    if go:
        change = {"origin": origin, "destination": dest, "start": start.isoformat(), "nights": int(nights),
                  "budget_currency": currency, **({"budget_amount": budget} if budget > 0 else {"no_budget": True})}
        with st.spinner("Searching flights and hotels…"):
            _preview(api_post, tid, change, {})
        st.rerun()


def _pick(label: str, options: list[dict], key: str, fmt) -> str | None:
    if not options:
        return None
    idx = next((i for i, o in enumerate(options) if o.get("chosen")), 0)
    choice = st.radio(label, range(len(options)), index=idx, format_func=lambda i: fmt(options[i]), key=key)
    return options[choice]


def proposal(api_post, prop: dict) -> None:
    res, n, tid = prop["result"], prop["n"], prop["trip"]
    if res["new_trip"]:
        st.warning(f"The route changes, so this will be saved as a **new trip** and Trip #{tid} will be shown "
                   "as cancelled (replaced by the new one). Undo can bring it back.", icon="🔁")

    st.markdown("##### Flights and hotel")
    st.caption(md("The usual choice is selected (best score: fare + $75 per stop + $20 per hour). Pick another if you prefer."))
    flight = lambda o: md(f"{o['label']} · score ${o['score']:,}" + (" ⚠️ " + "; ".join(o["problems"]) if o["problems"] else ""))
    with st.container(border=True):
        out = _pick("Outbound", res["options"]["outbound"], f"po-{tid}-{n}", flight)
        ret = _pick("Return", res["options"]["return"], f"pr-{tid}-{n}", flight)
        hotel = _pick("Hotel", res["options"]["hotels"], f"ph-{tid}-{n}", lambda h: md(h["label"]))
    picks = {"outbound": out["key"] if out else None, "return": ret["key"] if ret else None,
             "hotel": hotel["name"] if hotel else None}
    if picks != prop["picks"]:
        if st.button("Update preview with these choices", icon="🔄", use_container_width=True):
            with st.spinner("Re-pricing…"):
                _preview(api_post, tid, prop["change"], picks)
            st.rerun()
        st.info("Your choices changed. Update the preview to see the new totals before applying.", icon="ℹ️")
        return

    st.markdown("##### Review")
    rows = [r for r in res["diff"] if r["changed"] or r["field"] in ("Total", "Budget")]
    table = ["| | Before | After |", "|---|---|---|"]
    for r in rows:
        after = f"**{r['after']}**" if r["changed"] else r["after"]
        table.append(f"| {r['field']} | {r['before']} | {after} |")
    st.markdown(md("\n".join(table)))
    if not any(r["changed"] for r in res["diff"]):
        st.info("Nothing changes with these settings.", icon="ℹ️")
    if res["clashes"]:
        st.warning("The new dates overlap:  \n" + "  \n".join(
            f"{'🌴 ' if c.get('is_leave') else ''}{c['title']} — {c['start'].replace('T', ' ')}" for c in res["clashes"]),
            icon="📅")
    for e in res["effects"]:
        st.caption(f"• {e}")
    with st.expander("Preview the full new plan"):
        st.markdown(md(res["markdown"]))

    c1, c2 = st.columns(2)
    calendar = c1.checkbox("Update my calendar" if res["in_calendar"] else "Add the trip to my calendar",
                           value=res["in_calendar"], key=f"cal-{tid}-{n}")
    email = c2.checkbox("Save an *updated plan* email draft", value=True, key=f"mail-{tid}-{n}",
                        help="A draft to your manager with the new plan. It is never sent.")
    a, b = st.columns(2)
    if a.button("Apply changes", type="primary", use_container_width=True, icon="✅",
                disabled=not any(r["changed"] for r in res["diff"])):
        st.session_state.trip_confirm = {"n": n, "calendar": calendar, "email": email}
    ask = st.session_state.get("trip_confirm")
    if ask and ask["n"] == n:            # keep the question open until it's answered (survives reruns)
        confirm(api_post, prop, ask["calendar"], ask["email"])
    if b.button("Discard", use_container_width=True, icon="✖️"):
        st.session_state.pop("trip_prop", None)
        st.session_state.trip_msg = ("info", "Changes discarded. The trip is unchanged.")
        st.rerun()


def history(api_get, api_post, trip: dict) -> None:
    rows = api_get(f"/trips/{trip['id']}/history") or []
    if trip.get("replaced_by"):
        st.info(f"This trip was replaced by **Trip #{trip['replaced_by']}** (route changed).", icon="🔁")
    if not rows:
        st.caption("No earlier versions yet. Every applied change keeps the previous version here.")
        return
    for r in rows:
        with st.container(border=True):
            st.markdown(md(f"**{'Version ' + str(r['version']) if r['version'] else 'Origin'}** · {r['summary']}"))
            st.caption(f"Changed: {r['reason']} · saved {r['saved_at'][:16].replace('T', ' ')}")
    with st.popover("↩️ Undo the last change", use_container_width=True):
        st.write("Go back one step. Calendar events follow the restored version.")
        if st.button("Undo", type="primary", key=f"undo-{trip['id']}"):
            data, err = api_post(f"/trips/{trip['id']}/undo", {})
            st.session_state.trip_msg = ("error", err) if err else (
                "success", f"Undone: {data['label']}.  \n" + "  \n".join(f"✓ {a}" for a in data["actions"]))
            if not err:
                st.session_state.trip_sel = data["trip_id"]
            st.session_state.pop("trip_prop", None)
            st.rerun()


def render() -> None:
    st.title(":material/flight_takeoff: Trips")
    st.caption("Every trip the assistant planned. Change dates, nights, budget, flights or hotel, review the "
               "before/after, then apply: calendar events and the manager email are updated for you.")

    rows = api_get("/trips", {"limit": 200}) or []
    today = date.today().isoformat()
    which = st.segmented_control("Show", FILTERS, default=st.session_state.get("trip_filter", "Upcoming"),
                                 key="trip_filter_ctl", label_visibility="collapsed") or "Upcoming"
    _keep("trip_filter", which)
    shown = {"Upcoming": [r for r in rows if r["status"] != "cancelled" and r["end"] >= today],
             "Past": [r for r in rows if r["status"] != "cancelled" and r["end"] < today],
             "Cancelled": [r for r in rows if r["status"] == "cancelled"], "All": rows}[which]

    left, right = st.columns([1, 2.6], gap="large")
    with left:
        if not shown:
            st.caption("No trips here. Plan one in the chat, e.g. *Plan a 2-day trip to London next month*.")
        for r in sorted(shown, key=lambda r: r["start"]):
            selected = st.session_state.get("trip_sel") == r["id"]
            with st.container(border=True):
                st.markdown(md(f"**#{r['id']} · {r['origin']} → {r['destination']}**  \n"
                               f"{_dates(r['start'], r['end'])}"))
                st.markdown(badges(r["status"]) + (f" · ${r['total_usd']:,.0f}".replace("$", "\\$") if r.get("total_usd") else ""))
                if st.button("Selected" if selected else "Open", key=f"open-{r['id']}", use_container_width=True,
                             type="primary" if selected else "secondary", disabled=selected):
                    st.session_state.trip_sel = r["id"]
                    st.session_state.pop("trip_prop", None)
                    st.rerun()

    with right:
        if msg := st.session_state.pop("trip_msg", None):
            {"success": st.success, "error": st.error, "info": st.info}[msg[0]](md(msg[1]))
        tid = st.session_state.get("trip_sel")
        if not tid:
            st.info("Select a trip to see it or change it.", icon="👈")
            return
        trip = api_get(f"/trips/{tid}")
        if not trip:
            st.warning("That trip couldn't be loaded.")
            return
        plan = trip["plan"]
        st.markdown(md(f"### Trip #{tid} · {plan['origin']} → {plan['destination']}"))
        st.markdown(badges(trip["status"]) + (f" · :gray[{trip['versions']} earlier version(s)]" if trip["versions"] else ""))
        m = st.columns(4)
        s_, e_ = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
        m[0].metric("Dates", f"{s_:%d}–{e_:%d %b}" if s_.month == e_.month else f"{s_:%d %b}–{e_:%d %b}",
                    help=_dates(plan["start"], plan["end"]))
        m[1].metric("Nights", plan["nights"])
        m[2].metric("Total", f"${plan['costs']['total']:,.0f}")
        if plan.get("budget"):
            left_usd = plan["budget"] - plan["costs"]["total"]
            m[3].metric("Budget", f"${plan['budget']:,.0f}", f"{'+' if left_usd >= 0 else '-'}${abs(left_usd):,.0f}",
                        delta_color="normal" if left_usd >= 0 else "inverse")
        else:
            m[3].metric("Budget", "—")

        overview, edit, hist = st.tabs(["Overview", "Edit", "History"])
        with overview:
            st.markdown(md(trip["markdown"]))
        with edit:
            if trip["status"] == "cancelled":
                st.info("Cancelled trips can't be changed. Use **History → Undo** on the trip that replaced it, "
                        "or plan a new one.", icon="🔒")
            elif plan["end"] < today:
                st.info("Past trips can't be changed.", icon="🔒")
            else:
                prop = st.session_state.get("trip_prop")
                if prop and prop["trip"] == tid:
                    st.markdown(md(f"**Proposed change** · {', '.join(r['field'] for r in prop['result']['diff'] if r['changed']) or 'no changes'}"))
                    if st.button("✏️ Change the inputs again", key=f"again-{tid}"):
                        st.session_state.pop("trip_prop", None)
                        st.rerun()
                    proposal(api_post, prop)
                else:
                    edit_form(api_get, api_post, trip)
        with hist:
            history(api_get, api_post, trip)
