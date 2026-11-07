"""Trips agent: saved trips. List/count, compare flights, change nights or hotel, cancel.
Edits go through trip_service (the same path as the Trips page). Everything here is code, no model call."""
from __future__ import annotations

import re
from datetime import date, timedelta

from veltrix_copilot.travel import travel_rules, trip_service, trips
from veltrix_copilot.agents import calendar_agent, email_agent, plans, travel_agent
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.core.dates import MONTHS, fmt_day
from veltrix_copilot.tools.mcp_client import ToolError, call_tool

CANCEL_RE = re.compile(r"\bcancel\b", re.I)
COMPARE_RE = re.compile(r"\b(other|alternative|different|more|cheaper|faster|shorter|compare)\b[^.?!]{0,25}"
                        r"\b(flights?|options?)\b|\bcompare\b", re.I)
LENGTH_RE = re.compile(r"\b(\d{1,2})\s*(nights?|days?)\b", re.I)
HOTEL_RE = re.compile(r"\b(cheaper|less expensive|budget|better|nicer|higher[- ]rated|different|another|other)\s+hotel\b", re.I)
MAX_OPTIONS = 3


def intent(message: str) -> dict:
    if CANCEL_RE.search(message):
        return {"do": "cancel"}
    if m := HOTEL_RE.search(message):
        return {"do": "hotel", "value": "cheaper" if m.group(1).lower() in ("cheaper", "less expensive", "budget") else "better"}
    if COMPARE_RE.search(message):
        return {"do": "compare"}
    if (m := LENGTH_RE.search(message)) and re.search(r"\b(make|change|extend|shorten|set|instead|reduce|increase)\b", message, re.I):
        return {"do": "nights" if m.group(2).lower().startswith("night") else "days", "value": int(m.group(1))}
    return {"do": "list"}


def _month_window(message: str, today: date) -> tuple[set[tuple[int, int]], str]:
    """{(year, month)} the question asks about, and a label like 'November 2026'."""
    low = message.lower()
    shift = {"this month": 0, "next month": 1, "last month": -1}
    if rel := next((k for k in shift if k in low), None):
        m = today.month - 1 + shift[rel]
        ym = {(today.year + m // 12, m % 12 + 1)}
    else:
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", message)]
        months = plans.months_named(message)
        ym = {(y, m) for m in months for y in (years or [today.year if m >= today.month else today.year + 1])}
        if not months and years:
            ym = {(y, m) for y in years for m in range(1, 13)}
    if not ym:
        return set(), ""
    if len(ym) == 12 and len({y for y, _ in ym}) == 1:
        return ym, str(next(iter(ym))[0])
    names = {v: k for k, v in MONTHS.items() if len(k) > 3}
    return ym, " and ".join(f"{names[m].title()} {y}" for y, m in sorted(ym))


def _overlaps_month(t: dict, ym: set[tuple[int, int]]) -> bool:
    start, end = date.fromisoformat(t["start"]), date.fromisoformat(t["end"])
    return any((y, m) in ym for y in range(start.year, end.year + 1) for m in range(1, 13)
               if (start.year, start.month) <= (y, m) <= (end.year, end.month))


def list_trips(message: str, rows: list[dict], today: date) -> str:
    low = message.lower()
    ym, month_label = _month_window(message, today)
    dests = {r["destination"].lower() for r in rows if re.search(rf"\b{re.escape(r['destination'].lower())}\b", low)}
    want_cancelled = "cancel" in low
    hidden_cancelled = 0 if want_cancelled else sum(1 for r in rows if r.get("cancelled_at"))
    filters = [(True, lambda r: bool(r.get("cancelled_at")) == want_cancelled),
               (dests, lambda r: r["destination"].lower() in dests),
               (ym, lambda r: _overlaps_month(r, ym)),
               (re.search(r"\b(upcoming|future|coming|next)\b", low) and "next month" not in low,
                lambda r: r["end"] >= today.isoformat()),
               (re.search(r"\b(past|previous|completed|earlier)\b", low), lambda r: r["end"] < today.isoformat()),
               (re.search(r"\bnot (yet )?emailed\b", low), lambda r: not r["draft_ids"]),
               (not re.search(r"\bnot (yet )?emailed\b", low) and re.search(r"\bemailed\b", low), lambda r: r["draft_ids"]),
               (re.search(r"\bnot (yet )?in (my |the )?calendar\b", low), lambda r: not r["calendar_events"]),
               (not re.search(r"\bnot (yet )?in (my |the )?calendar\b", low) and re.search(r"\bin (my |the )?calendar\b", low),
                lambda r: r["calendar_events"])]
    selected = [r for r in rows if all(f(r) for on, f in filters if on)]

    what = ("cancelled " if want_cancelled else "") + "trip"
    where = f" to {'/'.join(sorted(d.title() for d in dests))}" if dests else ""
    when = f" travelling in {month_label}" if month_label else ""
    n = len(selected)
    lines = [f"**{n} {what}{'s' if n != 1 else ''}{where}{when}.**" if n else f"No {what}s{where}{when}."]
    if selected:
        lines += ["", "| Trip | Route | Dates | Total | Status |", "|---|---|---|---|---|"]
        for r in sorted(selected, key=lambda r: r["start"]):
            total = (r["plan"].get("costs") or {}).get("total")
            s, e = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
            lines.append(f"| #{r['id']} | {r['origin']} → {r['destination']} | {s:%d %b}–{e:%d %b %Y} | "
                         f"{f'${total:,.0f}' if total else '–'} | {trips.status(r)} |")
    # "planned in November" can also mean *created* in November: say so when the two readings differ.
    if ym and re.search(r"\b(planned|created|made|booked|added)\b", low):
        made = [r for r in rows if not r.get("cancelled_at") and (int(r["created_at"][:4]), int(r["created_at"][5:7])) in ym]
        if made and {r["id"] for r in made} != {r["id"] for r in selected}:
            lines += ["", f"_Counting by travel dates. By the day they were planned, {len(made)} trip"
                          f"{'s were' if len(made) != 1 else ' was'} planned in {month_label}: "
                          f"{', '.join('#' + str(r['id']) for r in made)}._"]
    if hidden_cancelled:
        lines += ["", f"_{hidden_cancelled} cancelled trip{'s are' if hidden_cancelled != 1 else ' is'} not counted._"]
    if selected:
        lines += ["", f"_Refer to a trip by number, e.g. *Show me other flights for trip #{selected[0]['id']}* "
                      f"or *Cancel trip #{selected[0]['id']}*._"]
    return "\n".join(lines)


def _option_rows(options: list[dict], current: dict, first: int) -> list[str]:
    key = travel_agent.flight_key(current)
    return [f"| **{i}** | {f['flight_no']} {f['airline']} | {travel_agent._hm(f['depart'])} → {travel_agent._hm(f['arrive'])} | "
            f"{travel_agent.stops_text(f)} | ${f['price_usd']:,} | {f.get('duration_hours', 0):.1f}h | "
            f"${travel_agent.flight_score(f):,.0f}{' ★ current' if travel_agent.flight_key(f) == key else ''} |"
            for i, f in enumerate(options, first)]


async def compare(plan: dict) -> AgentResult:
    ret_day = travel_agent._dt(plan["return"]["depart"]).date()
    outs = travel_agent.rank_options(
        await travel_agent._flights(plan["origin"], plan["destination"], date.fromisoformat(plan["start"])), n=MAX_OPTIONS)
    rets = travel_agent.rank_options(await travel_agent._flights(plan["destination"], plan["origin"], ret_day),
                                     travel_agent.earliest_return(travel_agent._dt(plan["outbound"]["arrive"]), ret_day),
                                     n=MAX_OPTIONS)
    head = "| # | Flight | Times | Stops | Fare | Time | Score |\n|---|---|---|---|---:|---:|---:|"
    text = "\n".join([
        f"**Flight options for {plans.plan_label(plan)}**",
        f"_Score = fare + ${travel_rules.STOP_COST_USD} per stop + ${travel_rules.HOUR_COST_USD} "
        "per hour of travel (lower is better); planner settings, not policy._", "",
        f"**Outbound, {fmt_day(date.fromisoformat(plan['start']))}**", "", head, *_option_rows(outs, plan["outbound"], 1), "",
        f"**Return, {fmt_day(ret_day)}**", "", head, *_option_rows(rets, plan["return"], len(outs) + 1), "",
        f"Reply with the option numbers to switch, e.g. **2**, or **2 and {len(outs) + 1}** for both directions. "
        "Anything else leaves the trip as it is."])
    action = {"do": "options", "out": outs, "ret": rets}
    return AgentResult(text=text, memory_updates={
        "pending": plans.pending_options(plan, "trips", len(outs) + len(rets), action), **plans.used(plan)})


async def _change(plan: dict, action: dict, state: dict) -> tuple[dict, str] | str:
    """The trip_service change for an action, and how to describe it; or a message why it can't be done."""
    do = action["do"]
    if do in ("nights", "days"):
        nights = action["value"] - (do == "days")
        if not 1 <= nights <= 14:
            return "Trips can be 1 to 14 nights or days."
        return {"nights": max(1, nights)}, f"now {max(1, nights)} night{'s' if nights != 1 else ''}"
    if do == "hotel":
        hotels = await call_tool("travel", "search_hotels", {"city": plan["destination"], "check_in": plan.get("arrive_day") or plan["start"],
                                                             "nights": plan["nights"], "max_price_per_night": plan["hotel_cap"]})
        current = plan.get("hotel") or {}
        names = [h["name"] for h in hotels]
        if action.get("value") == "better":
            pos = names.index(current["name"]) if current.get("name") in names else len(hotels)
            if pos == 0:
                return f"{current.get('name', 'This hotel')} is already the best-rated hotel within the policy cap."
            pick = hotels[pos - 1] if pos < len(hotels) else hotels[0]
        else:
            pick = next((h for h in hotels if h["price_per_night"] < current.get("price_per_night", 10 ** 9)), None)
            if not pick:
                return f"{current.get('name', 'This hotel')} is already the cheapest within the policy cap."
        return {"plan": {**plan, "hotel": pick}}, f"hotel changed to {pick['name']} (${pick['price_per_night']}/night)"
    # options picked after a comparison ("2" / "2 and 5"): those exact flights, re-priced by trip_service
    outs, rets, new, changed = action.get("out") or [], action.get("ret") or [], dict(plan), []
    for n in (state.get("confirmation") or {}).get("picks") or []:
        label = "outbound" if n <= len(outs) else "return"
        new[label] = outs[n - 1] if label == "outbound" else rets[n - len(outs) - 1]
        changed.append(f"{label} flight {new[label]['flight_no']}")
    landed, leaves = travel_agent._dt(new["outbound"]["arrive"]), travel_agent._dt(new["return"]["depart"])
    if leaves < landed + timedelta(hours=travel_rules.MIN_GROUND_HOURS):
        return (f"That outbound flight lands at {landed:%a %d %b %H:%M}, less than {travel_rules.MIN_GROUND_HOURS} "
                "hours before the return leaves. Pick a later return as well.")
    new.update(arrive_day=landed.date().isoformat(), nights=max(1, (leaves.date() - landed.date()).days),
               start=travel_agent._dt(new["outbound"]["depart"]).date().isoformat(), end=leaves.date().isoformat())
    return {"plan": new}, "switched " + " and ".join(changed)


async def edit(plan: dict, action: dict, state: dict) -> AgentResult:
    if not plan.get("trip_id"):
        return AgentResult(text="This trip isn't saved yet. Reply *save this trip* first, then I can change it.")
    got = await _change(plan, action, state)
    if isinstance(got, str):
        return AgentResult(text=got, memory_updates=plans.used(plan))
    change, reason = got
    row = await trips.call(trips.get, plan["trip_id"])
    in_calendar, emailed = bool(row["calendar_event_ids"]), bool(row["draft_ids"])
    try:
        res = await trip_service.apply(row["id"], change, row["updated_at"], email=emailed, calendar=in_calendar, reason=reason)
    except trip_service.TripError as e:
        return AgentResult(text=e.message, memory_updates=plans.used(plan))
    new = (await trips.call(trips.get, res["trip_id"]))["plan"]
    extra = (["📅 Calendar updated: the old flight and hotel events were replaced."] if in_calendar else []) + \
            (["📧 An *updated plan* email draft to your manager was saved (not sent)."] if emailed else [])
    memory = state.get("memory") or {}
    updates = {"chat_trips": plans.remember_trip(memory, new), **plans.used(new)}
    if memory.get("last_travel_plan") and plans.trip_key(memory["last_travel_plan"]) == plans.trip_key(new):
        updates["last_travel_plan"] = new
    text = f"**Trip updated: {reason}.**\n\n" + travel_agent.render_plan(new) + "".join(f"\n\n{x}" for x in extra)
    return AgentResult(text=text, actions=res["actions"], memory_updates=updates)


async def cancel(plan: dict, confirmed: bool) -> AgentResult:
    if plan.get("cancelled"):
        return AgentResult(text=f"**{plans.plan_label(plan)}** is already cancelled.")
    row = await trips.try_call(trips.get, plan["trip_id"]) if plan.get("trip_id") else None
    events, emailed = (row["calendar_event_ids"], row["draft_ids"]) if row else ([], [])
    if not confirmed:
        what = ([f"remove its {len(events)} calendar event{'s' if len(events) != 1 else ''}"] if events else []) + \
               (["draft a short *trip cancelled* email to your manager"] if emailed else [])
        detail = f" This will {' and '.join(what)}." if what else ""
        return AgentResult(text=f"Cancel **{plans.plan_label(plan)}**?{detail} Reply **yes** to cancel, or **no** to keep it.",
                           memory_updates={"pending": plans.pending_question(plan, "trips", {"do": "cancel"})})
    actions = []
    if events:
        actions.append(f"Removed {await calendar_agent.remove_events(events)} calendar event(s)")
        await trips.try_call(trips.clear_calendar, plan["trip_id"])
    if plan.get("trip_id"):
        await trips.try_call(trips.cancel, plan["trip_id"])
    if emailed:
        actions += (await email_agent.draft_trip_cancellation(plan))[1]
    actions.append(f"Cancelled Trip #{plan['trip_id']}" if plan.get("trip_id") else "Cancelled the trip")
    return AgentResult(text=f"**{plans.plan_label(plan)}** is cancelled. It stays in the trip list as *cancelled*, "
                            "so its number still works for reference.", actions=actions, memory_updates={"turn_trip": None})


async def perform(action: dict, plan: dict, state: dict, confirmed: bool = False) -> AgentResult:
    do = action.get("do")
    if plan.get("cancelled") and do != "cancel":
        return AgentResult(text=f"**{plans.plan_label(plan)}** was cancelled, so it can't be changed.")
    if do == "cancel":
        return await cancel(plan, confirmed)
    if do == "compare":
        return await compare(plan)
    if do in ("options", "nights", "days", "hotel"):
        return await edit(plan, action, state)
    return AgentResult(text="I'm not sure what to change. Try *make it 3 nights*, *pick a cheaper hotel* or *show me other flights*.")


async def run(message: str, state: dict) -> AgentResult:
    try:
        if conf := state.get("confirmation"):                 # an answer to our own question
            answer, plan = await plans.confirmed_plan(state)
            if answer == "no":
                return AgentResult(text="OK, nothing changed.")
            if not plan:
                return AgentResult(text="I couldn't find that trip any more.")
            return await perform(conf.get("action") or {}, plan, state, confirmed=True)
        want = intent(message)
        if want["do"] == "list":
            rows = await trips.try_call(trips.list_trips, 500)
            if rows is None:
                return AgentResult(text="The trip list isn't available: the database isn't reachable. "
                                        "Is Postgres running? (docker compose up -d db)")
            return AgentResult(text=list_trips(message, rows, date.today()))
        decision = await plans.resolve(message, state, "trips")
        if ask := plans.question(decision, "trips", want):
            return ask
        if decision.plan is None:
            return AgentResult(text="There are no saved trips yet. Plan one first, e.g. "
                                    "*Plan a 2-day trip to London next month under $1500.*")
        return await perform(want, decision.plan, state)
    except ToolError as e:
        return AgentResult(text=f"That didn't work: {e}")
