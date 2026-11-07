"""Calendar agent: a saved trip's flights and hotel (built in code), or one event read from the message (one LLM call)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from veltrix_copilot.core import llm
from veltrix_copilot.travel import trips
from veltrix_copilot.agents import plans
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.core.dates import find_time, has_date_hint, parse_time, resolve_start_date
from veltrix_copilot.tools.mcp_client import ToolError, call_tool
from veltrix_copilot.core.schemas import EventRequest
from veltrix_copilot.core.util import to_utc

EVENT_PROMPT = """Extract one calendar event from the user's request as JSON. Today is {today}.
title = short event title. date = YYYY-MM-DD if stated. start_time = HH:MM (24h) if stated."""


def _flight_event(f: dict, note: str) -> dict:
    from veltrix_copilot.agents.travel_agent import route_text, stops_text
    route = f"\nRoute: {route_text(f)}" if f.get("stops") else ""
    return {"title": f"Flight {f['flight_no']}: {f['origin']} → {f['destination']}", "start": f["depart"],
            "end": f["arrive"], "notes": f"{f['airline']} · {stops_text(f)}.{route}\n{note}",
            "start_tz": f.get("depart_tz", ""), "end_tz": f.get("arrive_tz", "")}


def events_from_plan(plan: dict) -> list[dict]:
    """Outbound, hotel (from landing, 15:00 at the earliest, to 11:00 or 3 h before the return), return."""
    o, r, h = plan["outbound"], plan["return"], plan["hotel"]
    note = "Prices/times from Google Flights via SerpAPI; NOT booked."
    events = [_flight_event(o, note)]
    if h:
        landed, leaves = datetime.fromisoformat(o["arrive"]), datetime.fromisoformat(r["depart"])
        check_in = max(landed, landed.replace(hour=15, minute=0))
        check_out = max(min(leaves.replace(hour=11, minute=0), leaves - timedelta(hours=3)), check_in + timedelta(hours=1))
        tz = o.get("arrive_tz", "")
        events.append({"title": f"Hotel: {h['name']} ({plan['destination']})", "start": f"{check_in:%Y-%m-%dT%H:%M}",
                       "end": f"{check_out:%Y-%m-%dT%H:%M}", "notes": f"{h['area']}. {note}", "start_tz": tz, "end_tz": tz})
    return events + [_flight_event(r, note)]


async def event_from_text(message: str, today: date) -> list[dict]:
    req = await llm.astructured(EventRequest, [{"role": "system", "content": EVENT_PROMPT.format(today=today.isoformat())},
                                               {"role": "user", "content": message}])
    day = resolve_start_date(message, today, req.date if req else None)        # dates from code first
    start = datetime.combine(day, datetime.strptime(parse_time(find_time(message) or (req.start_time if req else None)),
                                                    "%H:%M").time())
    minutes = max(15, min(req.duration_minutes if req else 60, 600))
    return [{"title": ((req.title if req else "") or "Meeting")[:120], "start": f"{start:%Y-%m-%dT%H:%M}",
             "end": f"{start + timedelta(minutes=minutes):%Y-%m-%dT%H:%M}"}]


async def _save(events: list[dict]) -> tuple[str, list[str], list[str]]:
    """Create the events. Returns (text, actions, event ids)."""
    lines, added, ids = [], 0, []
    for ev in events:
        res = await call_tool("calendar", "create_event", {k: v for k, v in ev.items() if v})
        ids += [res["event_id"]] if res.get("event_id") else []
        tz = f" ({res['start_tz']})" if res.get("start_tz") else ""
        link = f" · [open]({res['html_link']})" if str(res.get("html_link") or "").startswith("https://") else ""
        dup = " _(already in calendar)_" if res.get("duplicate") else ""
        added += not res.get("duplicate")
        lines.append(f"- {res['title']} — {datetime.fromisoformat(res['start']):%a %d %b %H:%M}{tz}{dup}{link}")
    actions = [f"Added {added} event(s) to Google Calendar" if added else "Google Calendar already had these events"]
    return "**Google Calendar updated**\n\n" + "\n".join(lines), actions, ids


async def add_trip(trip: dict) -> tuple[str, list[str], list[str]]:
    """Add a trip's events and record their ids on the trip. Returns (text, actions, event ids)."""
    events = events_from_plan(trip)
    text, actions, ids = await _save(events)
    if trip.get("trip_id"):
        await trips.try_call(trips.mark_calendar, trip["trip_id"], len(events), ids)
    return f"**{plans.plan_label(trip)}**\n\n{text}", actions, ids


async def remove_events(event_ids: list[str]) -> int:
    removed = 0
    for event_id in event_ids:
        try:
            removed += bool((await call_tool("calendar", "delete_event", {"event_id": event_id})).get("deleted"))
        except ToolError:
            pass
    return removed


async def clashes(trip: dict) -> list[dict]:
    """Other events overlapping the trip, from leaving home to getting back."""
    o, r = trip["outbound"], trip["return"]
    found = await call_tool("calendar", "find_conflicts", {
        "start_utc": f"{to_utc(o['depart'], o.get('depart_tz', '')):%Y-%m-%dT%H:%M}",
        "end_utc": f"{to_utc(r['arrive'], r.get('arrive_tz', '')):%Y-%m-%dT%H:%M}"})
    return [e for e in found if not e["title"].startswith(("Flight ", "Hotel: "))]


def clash_text(trip: dict, found: list[dict]) -> str:
    lines = [f"- {'🌴 ' if e.get('is_leave') else ''}{e['title']} — {datetime.fromisoformat(e['start']):%a %d %b %H:%M}"
             + (" _(leave)_" if e.get("is_leave") else "") for e in found]
    leave = ", including leave" if any(e.get("is_leave") for e in found) else ""
    return (f"**{plans.plan_label(trip)}** overlaps {len(found)} event{'s' if len(found) > 1 else ''} in your calendar"
            f"{leave}:\n\n" + "\n".join(lines) + "\n\nAdd the trip anyway? Reply **yes** to add it, or **no** to leave it out.")


async def run(message: str, state: dict, today: date | None = None) -> AgentResult:
    force = ((state.get("confirmation") or {}).get("action") or {}).get("force")
    answer, trip = await plans.confirmed_plan(state)
    if answer == "no":
        return AgentResult(text="OK, I left the trip out of the calendar." if force else
                           "OK. What should I add? Tell me the title, date and time, e.g. *Add a meeting with Sara tomorrow at 3pm*.")
    if answer == "yes" and not trip:
        return AgentResult(text="I couldn't find that trip any more. Which trip should I add?")
    if answer is None:
        decision = await plans.resolve(message, state, "calendar")
        if ask := plans.question(decision, "calendar"):
            return ask
        if decision.ref is plans.Ref.PLAN:
            trip = decision.plan
        elif not has_date_hint(message):
            return AgentResult(text="There's no saved trip yet. Plan a trip first, or tell me the event's date and time."
                               if decision.ref is plans.Ref.NONE and plans.mentions_trip(message)
                               else "What date and time should I put in the calendar?")
    try:
        if not trip:
            text, actions, _ = await _save(await event_from_text(message, today or date.today()))
            return AgentResult(text=text, actions=actions)
        if trip.get("cancelled"):
            return AgentResult(text=f"**{plans.plan_label(trip)}** was cancelled, so I didn't add it.")
        if not force and (found := await clashes(trip)):       # warn and ask before adding over other events
            return AgentResult(text=clash_text(trip, found), memory_updates={
                "pending": plans.pending_question(trip, "calendar", {"force": True})})
        updates, saved = {}, []
        if not trip.get("trip_id"):           # planned in this chat but not saved yet: adding it saves it
            if trip_id := await trips.try_call(trips.add, trip, state.get("session_id")):
                trip = {**trip, "trip_id": trip_id}
                memory = {**(state.get("memory") or {}), "last_travel_plan": None}
                updates = {"last_travel_plan": trip, "chat_trips": plans.remember_trip(memory, trip), "pending": None}
                saved = [f"Saved Trip #{trip_id}"]
        text, actions, _ = await add_trip(trip)
        return AgentResult(text=text, actions=saved + actions, memory_updates={**updates, **plans.used(trip)})
    except ToolError as e:
        return AgentResult(text=f"I couldn't save the event: {e}")
