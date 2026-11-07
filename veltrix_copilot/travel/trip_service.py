"""Editing a saved trip: preview -> apply (or discard), with history and undo. Used by the Trips page
(through the API) and kept consistent with the chat's trips agent."""
from __future__ import annotations

import json
from datetime import date

from veltrix_copilot.core import currency
from veltrix_copilot.travel import trips
from veltrix_copilot.agents import calendar_agent, email_agent, travel_agent
from veltrix_copilot.agents.plans import plan_label
from veltrix_copilot.core.config import ROOT
from veltrix_copilot.core.dates import fmt_day
from veltrix_copilot.tools.mcp_client import ToolError, ToolUnavailable

CITIES_FILE = ROOT / "mcp_servers" / "data" / "cities.json"
CURRENCIES = ["USD", "INR", "GBP", "EUR"]


class TripError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def cities() -> list[str]:
    data = json.loads(CITIES_FILE.read_text(encoding="utf-8"))
    return sorted({c["name"] for c in data.values()})


def current_inputs(plan: dict) -> dict:
    return {"origin": plan["origin"], "destination": plan["destination"], "start": plan["start"],
            "nights": int(plan["nights"]),
            "budget_amount": plan.get("budget_amount") if plan.get("budget") else None,
            "budget_currency": plan.get("budget_currency") or "USD"}


def merge_inputs(plan: dict, change: dict, today: date) -> dict:
    """The trip's current inputs with the requested changes, validated."""
    inp = current_inputs(plan)
    for k in ("origin", "destination", "start", "nights", "budget_currency"):
        if change.get(k) not in (None, ""):
            inp[k] = change[k]
    if change.get("no_budget"):
        inp["budget_amount"] = None
    elif change.get("budget_amount") is not None:
        inp["budget_amount"] = float(change["budget_amount"])
    known = cities()
    for k in ("origin", "destination"):
        inp[k] = str(inp[k]).strip().title()
        if inp[k] not in known:
            raise TripError(422, f"Unknown city '{inp[k]}'. Choose one of: {', '.join(known)}")
    if inp["origin"] == inp["destination"]:
        raise TripError(422, "Origin and destination must be different cities")
    try:
        start = date.fromisoformat(str(inp["start"]))
    except ValueError:
        raise TripError(422, "Start date must be YYYY-MM-DD")
    if start < today:
        raise TripError(422, "The start date is in the past")
    inp["start"] = start.isoformat()
    inp["nights"] = int(inp["nights"])
    if not 1 <= inp["nights"] <= 14:
        raise TripError(422, "A trip can be 1 to 14 nights")
    if inp["budget_currency"] not in CURRENCIES:
        raise TripError(422, f"Budget currency must be one of {', '.join(CURRENCIES)}")
    if inp["budget_amount"] is not None and inp["budget_amount"] <= 0:
        raise TripError(422, "The budget must be more than zero")
    return inp


def _fl(f: dict) -> str:
    return (f"{f['flight_no']} {f['airline']}, {travel_agent._hm(f['depart'])} → {travel_agent._hm(f['arrive'])} "
            f"({travel_agent.stops_text(f)}), ${f['price_usd']:,}")


def _hotel(h: dict | None) -> str:
    return f"{h['name']}, ${h['price_per_night']}/night" if h else "none"


def _dates(p: dict) -> str:
    return f"{fmt_day(date.fromisoformat(p['start']))} – {fmt_day(date.fromisoformat(p['end']))}"


def _budget(p: dict) -> str:
    if not p.get("budget"):
        return "no budget"
    left = p["budget"] - p["costs"]["total"]
    return f"${p['budget']:,.0f}: " + (f"${left:,.0f} left" if left >= 0 else f"${-left:,.0f} over")


def _warnings(p: dict) -> str:
    warn = [c["text"] for c in p.get("checks") or [] if c["status"] == "warn"]
    return "; ".join(warn) if warn else "all OK"


def diff(old: dict, new: dict) -> list[dict]:
    rows = [("Route", f"{old['origin']} → {old['destination']}", f"{new['origin']} → {new['destination']}"),
            ("Dates", _dates(old), _dates(new)),
            ("Nights", str(old["nights"]), str(new["nights"])),
            ("Outbound", _fl(old["outbound"]), _fl(new["outbound"])),
            ("Return", _fl(old["return"]), _fl(new["return"])),
            ("Hotel", _hotel(old.get("hotel")), _hotel(new.get("hotel"))),
            ("Total", f"${old['costs']['total']:,.0f}", f"${new['costs']['total']:,.0f}"),
            ("Budget", _budget(old), _budget(new)),
            ("Policy warnings", _warnings(old), _warnings(new))]
    return [{"field": f, "before": b, "after": a, "changed": a != b} for f, b, a in rows]


def change_summary(rows: list[dict]) -> str:
    changed = [r["field"].lower() for r in rows if r["changed"] and r["field"] not in ("Total", "Budget", "Policy warnings")]
    return ", ".join(changed) + " changed" if changed else "no changes"


def _option(f: dict, chosen: dict) -> dict:
    return {"key": travel_agent.flight_key(f), "label": _fl(f), "score": round(travel_agent.flight_score(f)),
            "price_usd": f["price_usd"], "stops": f.get("stops", 0), "duration_hours": f.get("duration_hours"),
            "problems": travel_agent.connection_problems(f),
            "chosen": travel_agent.flight_key(f) == travel_agent.flight_key(chosen)}


async def _row(trip_id: int) -> dict:
    try:
        row = await trips.call(trips.get, trip_id)
    except trips.TripStoreUnavailable:
        raise TripError(503, "Database unavailable. Is Postgres running?")
    if not row:
        raise TripError(404, "No such trip")
    return row


def _editable(row: dict, today: date) -> None:
    if row.get("cancelled_at"):
        raise TripError(409, "Cancelled trips can't be changed")
    if row["end"] < today.isoformat():
        raise TripError(409, "Past trips can't be changed")


async def _propose(row: dict, change: dict, today: date) -> dict:
    _editable(row, today)
    old = row["plan"]
    if change.get("plan"):          # flights/hotel already chosen in the chat: just re-price them
        new = travel_agent.reprice({**change["plan"], "trip_id": row["id"]}, today)
        return {"plan": new, "options": {}, "inputs": current_inputs(new), "new_trip": False}
    inp = merge_inputs(old, change, today)
    budget_usd = (inp["budget_amount"] / currency.rate(inp["budget_currency"])) if inp["budget_amount"] else None
    picks = {k: v for k, v in (change.get("picks") or {}).items() if v}
    try:
        new, options = await travel_agent.plan_trip(
            origin=inp["origin"], destination=inp["destination"], start=date.fromisoformat(inp["start"]),
            days=inp["nights"] + 1, nights=inp["nights"], today=today, budget_usd=budget_usd,
            budget_amount=inp["budget_amount"], budget_currency=inp["budget_currency"], picks=picks,
            prefer_hotel=(old.get("hotel") or {}).get("name") if inp["destination"] == old["destination"] else None)
    except ToolUnavailable:
        raise TripError(503, "The travel tool server isn't running. Start it with `uv run scripts/dev.py`.")
    except ToolError as e:
        raise TripError(422, str(e))
    new_trip = (inp["origin"], inp["destination"]) != (old["origin"], old["destination"])
    new["plan_id"] = old.get("plan_id") if not new_trip else new["plan_id"]
    new["trip_id"] = None if new_trip else row["id"]
    new["itinerary"] = travel_agent.fallback_itinerary(new)          # the model rewrites it only on Apply
    return {"plan": new, "options": options, "inputs": inp, "new_trip": new_trip}


async def preview(trip_id: int, change: dict, today: date | None = None) -> dict:
    """Plan the changed trip and describe the change. Nothing is saved."""
    today = today or date.today()
    row = await _row(trip_id)
    p = await _propose(row, change, today)
    new, options = p["plan"], p["options"]
    in_calendar, emailed = bool(row["calendar_event_ids"]), bool(row["draft_ids"])
    try:
        clashes = await calendar_agent.clashes(new)
    except (ToolError, ToolUnavailable):
        clashes = []
    effects = []
    if p["new_trip"]:
        effects.append(f"The route changes, so this is saved as a new trip and Trip #{row['id']} is shown as cancelled "
                       f"(replaced by the new one).")
    else:
        effects.append(f"Trip #{row['id']} is updated; the current version is kept in its history (undo is possible).")
    if in_calendar:
        effects.append(f"Its {len(row['calendar_event_ids'])} calendar events are removed and replaced with the new ones.")
    return {
        "trip_id": row["id"], "base_updated_at": row["updated_at"], "new_trip": p["new_trip"], "inputs": p["inputs"],
        "plan": new, "markdown": travel_agent.render_plan({**new, "trip_id": None}),
        "diff": diff(row["plan"], new), "effects": effects, "clashes": clashes,
        "in_calendar": in_calendar, "emailed": emailed,
        "options": {"outbound": [_option(f, new["outbound"]) for f in options["outbound"]],
                    "return": [_option(f, new["return"]) for f in options["return"]],
                    "hotels": [{"name": h["name"], "label": f"{h['name']} ({h.get('area', '')}), ${h['price_per_night']}/night"
                                + (f", rated {h['rating']}" if h.get("rating") else ""),
                                "price_per_night": h["price_per_night"],
                                "chosen": bool(new.get("hotel")) and h["name"] == new["hotel"]["name"]}
                               for h in options["hotels"]]},
        "picks": {"outbound": travel_agent.flight_key(new["outbound"]), "return": travel_agent.flight_key(new["return"]),
                  "hotel": (new.get("hotel") or {}).get("name")},
    }


async def _replace_calendar(old_ids: list[str], old_trip_id: int | None, plan: dict) -> list[str]:
    actions = []
    if old_ids:
        removed = await calendar_agent.remove_events(old_ids)
        if old_trip_id:
            await trips.call(trips.clear_calendar, old_trip_id)
        actions.append(f"Removed {removed} old calendar event(s)")
    _, added, _ = await calendar_agent.add_trip(plan)
    return actions + added


async def apply(trip_id: int, change: dict, base_updated_at: str, email: bool = True, calendar: bool = True,
                today: date | None = None, reason: str | None = None) -> dict:
    """Recompute the preview on the server and save it. `calendar`: put the trip in the calendar (its old
    events are replaced). `email`: save an 'updated plan' draft to the manager."""
    today = today or date.today()
    row = await _row(trip_id)
    if row["updated_at"] != base_updated_at:
        raise TripError(409, "This trip was changed somewhere else since the preview. Reload it and try again.")
    p = await _propose(row, change, today)
    new, rows = p["plan"], diff(row["plan"], p["plan"])
    if not any(r["changed"] for r in rows):
        raise TripError(422, "Nothing changed")
    new["itinerary"] = await travel_agent.write_itinerary(new)
    actions: list[str] = []
    try:
        if p["new_trip"]:
            new_id = await trips.call(trips.add, {**new, "replaces": row["id"]}, row["session_id"])
            new.update(trip_id=new_id, replaces=row["id"])
            await trips.call(trips.update_plan, new_id, new)
            if row["calendar_event_ids"]:
                removed = await calendar_agent.remove_events(row["calendar_event_ids"])
                await trips.call(trips.clear_calendar, row["id"])
                actions.append(f"Removed {removed} calendar event(s) of Trip #{row['id']}")
            await trips.call(trips.cancel, row["id"], new_id)
            actions.insert(0, f"Saved Trip #{new_id}; Trip #{row['id']} cancelled (replaced)")
            if calendar:
                _, added, _ = await calendar_agent.add_trip(new)
                actions += added
            reason = reason or f"replaces Trip #{row['id']} ({row['plan']['origin']} → {row['plan']['destination']})"
        else:
            new_id, reason = row["id"], reason or change_summary(rows)
            await trips.call(trips.save_version, row["id"], row["plan"], reason)
            await trips.call(trips.update_plan, row["id"], new)
            actions.append(f"Updated Trip #{row['id']}")
            if calendar:
                actions += await _replace_calendar(row["calendar_event_ids"], row["id"], new)
            elif row["calendar_event_ids"]:          # asked not to keep it in the calendar: remove the old events
                removed = await calendar_agent.remove_events(row["calendar_event_ids"])
                await trips.call(trips.clear_calendar, row["id"])
                actions.append(f"Removed {removed} calendar event(s)")
        if email:
            _, mail = await email_agent.draft_trip_update(new, reason)
            actions += mail
    except trips.TripStoreUnavailable:
        raise TripError(503, "Database unavailable. Is Postgres running?")
    except ToolUnavailable:
        raise TripError(503, "A tool server isn't running. Start everything with `uv run scripts/dev.py`.")
    return {"trip_id": new_id, "new_trip": p["new_trip"], "actions": actions, "label": plan_label(new)}


async def undo(trip_id: int) -> dict:
    """Go back one step: restore the previous version (and its calendar events), or, for a trip that replaced
    another, cancel it and bring the original trip back."""
    row = await _row(trip_id)
    actions: list[str] = []
    try:
        if row["versions"]:
            prev = await trips.call(trips.pop_version, trip_id)
            plan = {**prev["plan"], "trip_id": trip_id}
            await trips.call(trips.update_plan, trip_id, plan)
            actions.append(f"Trip #{trip_id} restored to the version before: {prev['reason']}")
            if row["calendar_event_ids"] and not row.get("cancelled_at"):
                actions += await _replace_calendar(row["calendar_event_ids"], trip_id, plan)
            return {"trip_id": trip_id, "actions": actions, "label": plan_label(plan)}
        original = row["plan"].get("replaces")
        if original:
            old = await _row(int(original))
            in_calendar = bool(row["calendar_event_ids"])
            if in_calendar:
                removed = await calendar_agent.remove_events(row["calendar_event_ids"])
                await trips.call(trips.clear_calendar, trip_id)
                actions.append(f"Removed {removed} calendar event(s) of Trip #{trip_id}")
            await trips.call(trips.cancel, trip_id)
            await trips.call(trips.uncancel, old["id"])
            actions.append(f"Trip #{trip_id} cancelled; Trip #{old['id']} restored")
            if in_calendar:
                _, added, _ = await calendar_agent.add_trip(old["plan"])
                actions += added
            return {"trip_id": old["id"], "actions": actions, "label": plan_label(old["plan"])}
    except trips.TripStoreUnavailable:
        raise TripError(503, "Database unavailable. Is Postgres running?")
    except ToolUnavailable:
        raise TripError(503, "The calendar tool server isn't running.")
    raise TripError(409, "Nothing to undo: this trip has no earlier version")


async def history(trip_id: int) -> list[dict]:
    row = await _row(trip_id)
    versions = await trips.call(trips.list_versions, trip_id)
    out = [{"version": len(versions) - i, "reason": v["reason"], "saved_at": v["created_at"],
            "summary": f"{v['plan']['origin']} → {v['plan']['destination']}, {_dates(v['plan'])}, "
                       f"${v['plan']['costs']['total']:,.0f}"} for i, v in enumerate(versions)]
    if row["plan"].get("replaces"):
        out.append({"version": 0, "reason": f"replaces Trip #{row['plan']['replaces']}", "saved_at": row["created_at"],
                    "summary": f"created from Trip #{row['plan']['replaces']}"})
    return out
