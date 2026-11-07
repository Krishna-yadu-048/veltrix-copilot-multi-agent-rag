"""Travel agent: extract -> call MCP tools and decide in code -> the LLM writes only the day-by-day outline."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta

from veltrix_copilot.core import currency, llm
from veltrix_copilot.travel import policy_checks, travel_rules, trips
from veltrix_copilot.agents import plans as followups
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.core.config import settings
from veltrix_copilot.core.dates import fmt_day, resolve_start_date
from veltrix_copilot.tools.mcp_client import ToolError, call_tool
from veltrix_copilot.core.schemas import Itinerary, TripRequest
from veltrix_copilot.core.util import duration

log = logging.getLogger(__name__)

WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_NOT_PLACES = {"My", "The", "Our", "Next", "This", "Me", "Him", "Her", "Them", "It", "A", "An", "Plan", "Please"}

EXTRACT_PROMPT = """Extract trip details from the user's request as JSON.
Today is {today}. Only fill fields the user actually stated; use null otherwise.
destination = the city they want to travel to."""

ITINERARY_PROMPT = """Write a short day-by-day itinerary for a business trip.
Facts:
- Traveller flies {origin} -> {destination}: {out_flight}, departs {out_depart}, lands {out_arrive} ({out_stops}).
- Hotel: {hotel}, from the day they land.
- Return: {ret_flight}, departs {ret_depart} ({ret_stops}).
- Days: {day_list}
Return JSON {{"days": [...]}} with exactly one string per day, in the form "Day N (<date>): ..." with at most 2 sentences each.
Do NOT mention any prices, costs, currency, or budget."""


def _num(s: str) -> float:
    return float(s.replace(",", ""))


_CUR_WORDS = {
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
}
_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)"


def parse_budget(t: str) -> tuple[float, str] | None:
    """'$1500' / '₹1,20,000' / '90000 rupees' / 'budget of 1500' -> (amount, currency code)."""
    m = re.search(r"(₹|\$|£|€|\brs\.?|\binr|\busd|\bgbp|\beur)\s?" + _AMOUNT, t)
    if m:
        return _num(m.group(2)), _CUR_WORDS[m.group(1).strip()]
    m = re.search(_AMOUNT + r"\s*(usd|dollars?|inr|rupees?|rs\.?|gbp|pounds?|eur|euros?)\b", t)
    if m and len(m.group(1).replace(",", "")) >= 3:
        return _num(m.group(1)), _CUR_WORDS[m.group(2)]
    m = re.search(r"\b(?:budget\s+(?:of\s+|is\s+|around\s+)?|under\s+|below\s+|within\s+|max(?:imum)?\s+)" + _AMOUNT, t)
    if m and len(m.group(1).replace(",", "")) >= 3:
        return _num(m.group(1)), "USD"
    return None


def parse_trip_text(text: str) -> dict:
    """Deterministic extraction of the easy fields. The LLM only fills what this misses."""
    out: dict = {}
    t = text.lower()

    if budget := parse_budget(t):
        amount, code = budget
        out["budget_amount"], out["budget_currency"] = amount, code
        out["budget_usd"] = round(amount / currency.rate(code), 2)

    if m := re.search(r"\b(\d{1,2})[\s-]*days?\b", t):
        out["days"] = int(m.group(1))
    elif m := re.search(r"\b(" + "|".join(WORD_NUMS) + r")[\s-]*days?\b", t):
        out["days"] = WORD_NUMS[m.group(1)]
    if m := re.search(r"\b(\d{1,2})\s*nights?\b", t):
        out["nights"] = int(m.group(1))

    place = r"([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)"
    for pat in (rf"\b(?:trip|travel|fly|flight|flights|go|going|visit)\s+to\s+{place}", rf"\bto\s+{place}", rf"\bvisit(?:ing)?\s+{place}"):
        for m in re.finditer(pat, text):
            candidate = m.group(1)
            first = candidate.split()[0]
            if first not in _NOT_PLACES:
                # Drop a trailing capitalised word that is not part of the city ("London Next")
                words = [w for w in candidate.split() if w not in _NOT_PLACES]
                out["destination"] = " ".join(words)
                break
        if "destination" in out:
            break
    if m := re.search(rf"\bfrom\s+{place}", text):
        if m.group(1).split()[0] not in _NOT_PLACES:
            out["origin"] = m.group(1)
    return out


async def extract_trip(message: str, today: date) -> dict:
    fields = parse_trip_text(message)
    if not fields.get("destination"):
        # Only call the model when the cheap parser could not find the destination.
        req = await llm.astructured(
            TripRequest,
            [{"role": "system", "content": EXTRACT_PROMPT.format(today=today.isoformat())},
             {"role": "user", "content": message}],
        )
        if req:
            for k, v in req.model_dump().items():
                if v not in (None, "") and k not in fields:
                    fields[k] = v
    return fields


def trip_length(fields: dict) -> tuple[int, int]:
    """Return (days, nights). An N-day trip = N-1 nights (minimum 1)."""
    if fields.get("nights"):
        nights = max(1, int(fields["nights"]))
        return nights + 1, nights
    days = max(1, min(int(fields.get("days") or 2), 14))
    return days, max(1, days - 1)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def flight_score(f: dict) -> float:
    """Price plus the planner's cost of each stop and each hour of travel (lower is better)."""
    return (f["price_usd"] + travel_rules.STOP_COST_USD * f.get("stops", 0)
            + travel_rules.HOUR_COST_USD * float(f.get("duration_hours") or 0))


def connection_problems(f: dict) -> list[str]:
    """Why a flight breaks the planner's connection rules (empty list = fine)."""
    problems = []
    if f.get("stops", 0) > travel_rules.MAX_STOPS:
        problems.append(f"{f['stops']} stops")
    for lay in f.get("layovers") or []:
        m = lay.get("minutes")
        where = lay.get("name") or lay.get("airport") or "the connection"
        if m is not None and m < travel_rules.MIN_LAYOVER_MINUTES:
            problems.append(f"only {duration(m)} to connect in {where}")
        elif m is not None and m > travel_rules.MAX_LAYOVER_MINUTES:
            problems.append(f"a {duration(m)} wait in {where}")
        elif lay.get("overnight"):
            problems.append(f"an overnight layover in {where}")
    return problems


def choose_flight(options: list[dict], not_before: datetime | None = None) -> tuple[dict | None, str]:
    """Best flight by flight_score among those that follow the connection rules.
    Overnight layovers are avoided when anything else exists. Returns (flight, note)."""
    usable = [f for f in options if not_before is None or _dt(f["depart"]) >= not_before]
    if not usable:
        return None, ""
    good = [f for f in usable if not connection_problems(f)]
    if good:
        return min(good, key=flight_score), ""
    best = min(usable, key=flight_score)
    return best, f"No flight met the connection rules; the best available has {', '.join(connection_problems(best))}."


def stops_text(f: dict) -> str:
    """'direct', '1 stop: Dubai (3h 10m)', '2 stops: Mumbai (2h 05m), Dubai (4h 00m)'."""
    lays = f.get("layovers") or []
    if not f.get("stops"):
        return "direct"
    where = ", ".join(f"{lay.get('name') or lay.get('airport')}"
                      + (f" ({duration(lay['minutes'])})" if lay.get("minutes") is not None else "") for lay in lays)
    return f"{f['stops']} stop{'s' if f['stops'] > 1 else ''}" + (f": {where}" if where else "")


def route_text(f: dict) -> str:
    """Leg by leg: 'COK 02:35 → BOM 04:30 (AI365) · 1h 53m in Mumbai · BOM 06:23 → LHR 11:59 (AI112)'."""
    legs, lays = f.get("legs") or [], f.get("layovers") or []
    parts = []
    for k, leg in enumerate(legs):
        parts.append(f"{leg['from']} {_dt(leg['depart']).strftime('%H:%M')} → {leg['to']} "
                     f"{_dt(leg['arrive']).strftime('%H:%M')} ({leg['flight_no']})")
        if k < len(lays) and lays[k].get("minutes") is not None:
            parts.append(f"{duration(lays[k]['minutes'])} in {lays[k].get('name') or lays[k].get('airport')}")
    return " · ".join(parts)


def choose_hotel(hotels: list[dict], max_per_night: float) -> dict | None:
    """Best hotel at or under the nightly limit (hotels arrive sorted best first)."""
    ok = [h for h in hotels if h["price_per_night"] <= max_per_night]
    return ok[0] if ok else None


def compute_costs(outbound: dict, ret: dict, hotel: dict | None, nights: int, days: int, per_diem_rate: int) -> dict:
    flights = outbound["price_usd"] + ret["price_usd"]
    hotel_total = hotel["price_per_night"] * nights if hotel else 0
    per_diem_total = per_diem_rate * days
    return {"flights": flights, "hotel": hotel_total, "per_diem": per_diem_total,
            "total": flights + hotel_total + per_diem_total}


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M")


def _home(plan: dict) -> str:
    return plan.get("home_currency") or "USD"


def _local(plan: dict) -> str:
    return plan.get("local_currency") or "USD"


def money(usd: float, plan: dict) -> str:
    """'$470' plus the home-currency equivalent when it differs, e.g. '$470 (≈ ₹41,360)'."""
    home = _home(plan)
    return f"${usd:,.0f}" + ("" if home == "USD" else f" (≈ {currency.fmt_usd_as(usd, home)})")


def _hotel_name(h: dict) -> str:
    """Hotel name, linked to its website when a live result provides an https link."""
    name = re.sub(r"[\[\]|]", "", h["name"])
    link = h.get("link") or ""
    return f"[{name}]({link})" if link.startswith("https://") and " " not in link and ")" not in link else name


def render_cost_table(plan: dict) -> str:
    o, r, h, c = plan["outbound"], plan["return"], plan["hotel"], plan["costs"]
    home = _home(plan)
    extra = home != "USD"

    def row(item: str, details: str, usd: float | None) -> str:
        if usd is None:
            return f"| {item} | {details} | – |" + (" – |" if extra else "")
        cells = f"| {item} | {details} | ${usd:,.0f} |"
        return cells + (f" {currency.fmt_usd_as(usd, home)} |" if extra else "")

    rows = [
        "| Item | Details | USD |" + (f" ≈ {home} |" if extra else ""),
        "|---|---|---:|" + ("---:|" if extra else ""),
        row("Outbound flight", f"{o['flight_no']} {o['airline']}, {_hm(o['depart'])} → {_hm(o['arrive'])} · {stops_text(o)}",
            o["price_usd"]),
        row("Return flight", f"{r['flight_no']} {r['airline']}, {_hm(r['depart'])} → {_hm(r['arrive'])} · {stops_text(r)}",
            r["price_usd"]),
        row("Hotel", f"{_hotel_name(h)} ({h['area']}), {plan['nights']} night(s) × ${h['price_per_night']:,}", c["hotel"])
        if h else row("Hotel", "No hotel found within the limits", None),
        row("Meals per diem", f"{plan['days']} day(s) × ${plan['per_diem_rate']}", c["per_diem"]),
    ]
    total = f"| **Total** | | **${c['total']:,.0f}** |"
    rows.append(total + (f" **{currency.fmt_usd_as(c['total'], home)}** |" if extra else ""))
    return "\n".join(rows)


def local_currency_line(plan: dict) -> str:
    """Spending money on the ground, in the destination's currency."""
    local = _local(plan)
    if local in ("USD", _home(plan)):
        return ""
    lim = plan.get("policy") or {}
    in_policy_currency = lim.get("covered") and travel_rules.PER_DIEM[lim["region"]][1] == local   # already shown
    per_diem = "" if in_policy_currency else f" · meals per diem ≈ {currency.fmt_usd_as(plan['per_diem_rate'], local)}/day"
    return (f"Local currency in {plan['destination']}: {local}{per_diem} · hotel ≈ "
            f"{currency.fmt_usd_as(plan['costs']['hotel'], local)} in total.")


def budget_line(plan: dict) -> str:
    b, total = plan.get("budget"), plan["costs"]["total"]
    if not b:
        return "No budget given."
    code = plan.get("budget_currency") or "USD"
    if code == "USD":
        given, left, over = f"${b:,.0f}", f"${b - total:,.0f}", f"${total - b:,.0f}"
    else:
        given = f"{currency.fmt(plan['budget_amount'], code)} (≈ ${b:,.0f})"
        left, over = currency.fmt_usd_as(b - total, code), currency.fmt_usd_as(total - b, code)
    if total <= b:
        return f"✅ Within your {given} budget ({left} left)."
    return f"⚠️ Over your {given} budget by {over}. Consider fewer nights or different dates."


def plan_summary_text(plan: dict) -> str:
    """Plain-text summary (used in email bodies). Numbers come from code only."""
    o, r, h, c = plan["outbound"], plan["return"], plan["hotel"], plan["costs"]
    budget = ""
    if plan.get("budget"):
        code = plan.get("budget_currency") or "USD"
        budget = (f" (budget ${plan['budget']:,.0f})" if code == "USD"
                  else f" (budget {currency.fmt(plan['budget_amount'], code)})")
    lines = [
        f"{_trip_title(plan).replace(' plan', '')}: {plan['origin']} -> {plan['destination']}",
        f"Dates: {fmt_day(date.fromisoformat(plan['start']))} to {fmt_day(date.fromisoformat(plan['end']))} ({plan['nights']} night(s))",
        f"Outbound: {o['flight_no']} {o['airline']}, {_hm(o['depart'])} -> {_hm(o['arrive'])} ({stops_text(o)}) - {money(o['price_usd'], plan)}",
        *([f"  Route: {route_text(o).replace('→', '->')}"] if o.get("stops") else []),
        f"Return: {r['flight_no']} {r['airline']}, {_hm(r['depart'])} -> {_hm(r['arrive'])} ({stops_text(r)}) - {money(r['price_usd'], plan)}",
        *([f"  Route: {route_text(r).replace('→', '->')}"] if r.get("stops") else []),
        (f"Hotel: {h['name']} ({h['area']}) - {plan['nights']} night(s) x ${h['price_per_night']:,} = {money(c['hotel'], plan)}"
         if h else "Hotel: to be confirmed"),
        f"Meals per diem: {plan['days']} day(s) x ${plan['per_diem_rate']} = {money(c['per_diem'], plan)}",
        f"Estimated total: {money(c['total'], plan)}{budget}",
    ]
    if _home(plan) != "USD":
        lines.append("(Prices were searched in USD; other currencies use fixed indicative rates.)")
    if plan.get("checks"):
        lines += ["", "Policy checks:"] + [f"- {ln}" for ln in policy_checks.render_text(plan["checks"])]
    return "\n".join(lines)


_MONEY_RE = re.compile(
    r"[$€£₹¥]|\b(usd|inr|eur|gbp|jpy|aed|sgd|rs\.?|rupees?|dollars?|pounds?|euros?|yen|dirhams?|"
    r"budget|costs?|price[sd]?|fares?|rates?|spend\w*|expensive|cheap\w*|afford\w*)\b", re.I)


def sanitize_itinerary(text: str) -> str:
    """Drop any line where the model mentioned money (the table is the only source of numbers)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(ln for ln in lines if not _MONEY_RE.search(ln))


def trip_dates(plan: dict) -> list[date]:
    """Every calendar date from the outbound departure to the return departure."""
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def fallback_itinerary(plan: dict) -> str:
    """Template itinerary built from the real times (used when the model is unavailable)."""
    o, r = plan["outbound"], plan["return"]
    hotel = plan["hotel"]["name"] if plan["hotel"] else "your hotel"
    land, leave = _dt(o["arrive"]), _dt(r["depart"])
    via = f" via {', '.join(lay.get('name') or lay.get('airport') for lay in o.get('layovers') or [])}" if o.get("stops") else ""
    lines = []
    for n, d in enumerate(trip_dates(plan), 1):
        parts = []
        if d == _dt(o["depart"]).date():
            parts.append(f"Depart {plan['origin']} at {_dt(o['depart']):%H:%M}{via}.")
        if d == land.date():
            parts.append(f"Land in {plan['destination']} at {land:%H:%M} and check in at {hotel}.")
        if d == leave.date():
            parts.append(f"Check out and fly home at {leave:%H:%M}.")
        elif not parts:
            parts.append(f"Meetings and work in {plan['destination']}.")
        lines.append(f"Day {n} ({fmt_day(d)}): " + " ".join(parts))
    return "\n".join(lines)


async def write_itinerary(plan: dict) -> str:
    day_list = ", ".join(f"Day {i + 1} = {fmt_day(d)}" for i, d in enumerate(trip_dates(plan)))
    h, o, r = plan["hotel"], plan["outbound"], plan["return"]
    prompt = ITINERARY_PROMPT.format(
        origin=plan["origin"], destination=plan["destination"],
        out_flight=f"{o['flight_no']} {o['airline']}", out_depart=_hm(o["depart"]), out_arrive=_hm(o["arrive"]),
        out_stops=stops_text(o),
        hotel=f"{h['name']} in {h['area']}" if h else "not booked yet",
        ret_flight=f"{r['flight_no']} {r['airline']}", ret_depart=_hm(r["depart"]), ret_stops=stops_text(r),
        day_list=day_list,
    )
    try:
        # JSON output (one string per day): model reasoning can't leak into the itinerary.
        result = await llm.astructured(Itinerary, [{"role": "user", "content": prompt}], num_predict=450)
        text = sanitize_itinerary("\n".join(result.days)) if result else ""
    except llm.LLMUnavailable as e:
        log.warning("Itinerary LLM call failed: %s", e)
        text = ""
    return text if text.count("Day") >= 1 else fallback_itinerary(plan)


async def _flights(origin: str, destination: str, day: date) -> list[dict]:
    return await call_tool("travel", "search_flights", {"origin": origin, "destination": destination,
                                                         "date": day.isoformat()})


def earliest_return(landed: datetime, ret_day: date) -> datetime:
    """The return leaves MIN_GROUND_HOURS after landing, and not before EARLIEST_RETURN_HOUR on its day."""
    return max(landed + timedelta(hours=travel_rules.MIN_GROUND_HOURS),
               datetime.combine(ret_day, datetime.min.time()) + timedelta(hours=travel_rules.EARLIEST_RETURN_HOUR))


def flight_key(f: dict) -> str:
    """Identifies one flight option (flight numbers + departure time), e.g. for a choice made in the UI."""
    return f"{f['flight_no']}|{f['depart']}"


def rank_options(options: list[dict], not_before: datetime | None = None, n: int = 5) -> list[dict]:
    """Options worth showing: those that follow the connection rules first, each group best score first."""
    usable = [f for f in options if not_before is None or _dt(f["depart"]) >= not_before]
    return sorted(usable, key=lambda f: (bool(connection_problems(f)), flight_score(f)))[:n]


def _picked(options: list[dict], key: str, what: str) -> dict:
    for f in options:
        if flight_key(f) == key:
            return f
    raise ToolError(f"The chosen {what} flight is no longer offered for those dates")


async def build_plan(fields: dict, today: date, message: str) -> dict:
    days, nights = trip_length(fields)
    plan, _ = await plan_trip(
        origin=(fields.get("origin") or settings.home_city).strip().title(),
        destination=fields["destination"].strip().title(),
        start=resolve_start_date(message, today, fields.get("start_date")),
        days=days, nights=nights, budget_usd=fields.get("budget_usd"),
        budget_amount=fields.get("budget_amount"), budget_currency=fields.get("budget_currency"), today=today)
    return plan


async def plan_trip(*, origin: str, destination: str, start: date, days: int, nights: int, today: date,
                    budget_usd: float | None = None, budget_amount: float | None = None,
                    budget_currency: str | None = None, picks: dict | None = None,
                    prefer_hotel: str | None = None) -> tuple[dict, dict]:
    """Plan a trip. `picks` can fix the outbound/return flight (flight_key) and the hotel (name), as chosen
    on the Trips page; anything not picked is chosen by the usual rules. Returns (plan, options), where
    options lists the flights and hotels that could be chosen instead."""
    picks = picks or {}
    notes: list[str] = []

    # Outbound: best by price, stops and travel time (connections within the planner's limits).
    out_options = await _flights(origin, destination, start)
    if picks.get("outbound"):
        outbound = _picked(out_options, picks["outbound"], "outbound")
        note = ("The chosen outbound flight has " + ", ".join(connection_problems(outbound)) + "."
                if connection_problems(outbound) else "")
    else:
        outbound, note = choose_flight(out_options)
    if outbound is None:
        raise ToolError("No flights found for those dates")
    notes += [note] if note else []

    # The stay starts when you LAND (a long-haul flight can arrive the next day), and the return
    # must leave at least MIN_GROUND_HOURS after landing. If no return flight fits, add a day.
    landed = _dt(outbound["arrive"])
    arrive_day = landed.date()
    if arrive_day > start:
        notes.append(f"You land on {fmt_day(arrive_day)}, so the hotel starts that day.")
    ret, ret_options, not_before = None, [], None
    for extra in range(1 if picks.get("return") else 3):
        ret_day = arrive_day + timedelta(days=nights + extra)
        not_before = earliest_return(landed, ret_day)
        try:
            ret_options = await _flights(destination, origin, ret_day)
        except ToolError as e:                       # no flights that day: try the next one
            if "No live flights found" not in str(e) or picks.get("return"):
                raise
            continue
        if picks.get("return"):
            ret = _picked(ret_options, picks["return"], "return")
            if _dt(ret["depart"]) < not_before:
                raise ToolError(f"The chosen return flight leaves less than {travel_rules.MIN_GROUND_HOURS} hours "
                                f"after you land, or before {travel_rules.EARLIEST_RETURN_HOUR:02d}:00")
            note = ""
        else:
            ret, note = choose_flight(ret_options, not_before=not_before)
        if ret is not None:
            if extra:
                notes.append(f"Extended by {extra} day{'s' if extra > 1 else ''}: no return flight left at least "
                             f"{travel_rules.MIN_GROUND_HOURS} hours after you land and after "
                             f"{travel_rules.EARLIEST_RETURN_HOUR:02d}:00 on the day of return.")
                nights, days = nights + extra, days + extra
            notes += [note] if note else []
            break
    if ret is None:
        raise ToolError(f"No return flight leaves at least {travel_rules.MIN_GROUND_HOURS} hours after you land")
    end = _dt(ret["depart"]).date()
    budget = float(budget_usd) if budget_usd else None

    limits = travel_rules.limits(destination)          # POL-FIN-001 caps for the destination, in USD and local
    cap, pd_rate = limits["hotel_cap_usd"], limits["per_diem_usd"]
    max_night = float(cap)
    if budget:
        remaining = budget - outbound["price_usd"] - ret["price_usd"] - pd_rate * days
        max_night = min(cap, remaining // nights) if remaining > 0 else 0

    hotels = await call_tool("travel", "search_hotels", {"city": destination, "check_in": arrive_day.isoformat(),
                                                          "nights": nights, "max_price_per_night": cap})
    if picks.get("hotel"):
        hotel = next((h for h in hotels if h["name"] == picks["hotel"]), None)
        if hotel is None:
            raise ToolError("The chosen hotel isn't available within the policy cap for those dates")
    elif prefer_hotel and (same := next((h for h in hotels if h["name"] == prefer_hotel), None)):
        hotel = same                                   # an edit keeps the hotel the trip already had
    else:
        hotel = choose_hotel(hotels, max_night)
        if hotel is None and hotels:  # nothing fits the budget: show the cheapest allowed by policy
            hotel = min(hotels, key=lambda h: h["price_per_night"])

    plan = {
        "plan_id": uuid.uuid4().hex[:8],
        "destination": destination, "origin": origin,
        "start": start.isoformat(), "end": end.isoformat(), "arrive_day": arrive_day.isoformat(),
        "days": days, "nights": nights, "notes": notes,
        "outbound": outbound, "return": ret, "hotel": hotel,
        "hotel_cap": cap, "per_diem_rate": pd_rate, "policy": limits, "budget": budget,
        "budget_currency": budget_currency or "USD",
        "budget_amount": budget_amount or budget,
        "home_currency": currency.home_currency(settings.home_city, settings.home_currency),
        "local_currency": currency.currency_for_city(destination),
        "searched_on": today.isoformat(),
    }
    plan = reprice(plan, today)
    chosen = rank_options(out_options)
    outs = chosen + [outbound] if all(flight_key(f) != flight_key(outbound) for f in chosen) else chosen
    rets = rank_options(ret_options, not_before)
    rets = rets + [ret] if all(flight_key(f) != flight_key(ret) for f in rets) else rets
    return plan, {"outbound": outs, "return": rets, "hotels": hotels}


def reprice(plan: dict, today: date) -> dict:
    """(Re)compute everything derived from the flights, hotel and nights: costs, budget, policy checks.
    Used for new plans and after a trip is edited."""
    o, r, h = plan["outbound"], plan["return"], plan["hotel"]
    plan["costs"] = compute_costs(o, r, h, plan["nights"], plan["days"], plan["per_diem_rate"])
    plan["within_budget"] = (plan["costs"]["total"] <= plan["budget"]) if plan.get("budget") else None
    plan["checks"] = policy_checks.check(plan, today)
    return plan


def itinerary_markdown(text: str) -> str:
    """One bullet per day (single newlines would be merged into one paragraph by Markdown)."""
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", ln).strip() for ln in text.splitlines()]
    return "\n".join(f"- {ln}" for ln in lines if ln)


def render_plan(plan: dict) -> str:
    plan = {**plan, "itinerary": itinerary_markdown(plan["itinerary"])}
    return (
        f"**{_trip_title(plan)}: {plan['origin']} → {plan['destination']}** "
        f"({fmt_day(date.fromisoformat(plan['start']))} – {fmt_day(date.fromisoformat(plan['end']))})\n\n"
        f"{plan['itinerary']}\n\n{render_cost_table(plan)}\n\n{route_lines(plan)}{budget_line(plan)}  \n"
        + policy_line(plan) + "  \n"
        + (f"{local}  \n" if (local := local_currency_line(plan)) else "")
        + (f"\n{checks}\n" if (checks := policy_checks.render_markdown(plan.get("checks") or [])) else "")
        + f"\n_Live prices from Google Flights / Google Hotels via SerpAPI, searched {plan.get('searched_on', '')}. "
        "Prices change; nothing is booked. Conversions use fixed indicative rates._"
    )


def policy_line(plan: dict) -> str:
    lim = plan.get("policy") or travel_rules.limits(plan["destination"])
    where = lim["label"] if lim["covered"] else f"{plan['destination']} (not covered: US amounts used as a guide)"
    usd = (lambda amount, n: "" if amount.startswith("$") else f" (≈ ${n})")
    return (f"Policy ({travel_rules.POLICY_ID}) for {where}: hotel cap {lim['hotel_cap']}/night"
            f"{usd(lim['hotel_cap'], lim['hotel_cap_usd'])}; per diem {lim['per_diem']}/day"
            f"{usd(lim['per_diem'], lim['per_diem_usd'])}.")


def route_lines(plan: dict) -> str:
    """Leg-by-leg routes for connecting flights, and any planning notes (empty when all direct)."""
    lines = [f"{label} route: {route_text(f)}" for label, f in (("Outbound", plan["outbound"]), ("Return", plan["return"]))
             if f.get("stops")]
    lines += [f"ℹ️ {n}" for n in plan.get("notes") or []]
    return "".join(f"{ln}  \n" for ln in lines) + ("\n" if lines else "")


def _trip_title(plan: dict) -> str:
    return f"Trip #{plan['trip_id']}" if plan.get("trip_id") else "Trip plan"


SAVE_QUESTION = ("**Save this trip?** Reply **Yes** to save it to your trips and add it to Google Calendar, "
                 "or **No** to discard it.")
SAVE_RE = re.compile(r"^\s*(?:please\s+)?(?:save|keep)\s+(?:it|this|that|the|my)?\s*(?:trip|plan|itinerary)?\b"
                     r"(?![^.?!]*\b(?:calendar|e-?mail|draft)\b)", re.I)


def is_save_request(message: str) -> bool:
    """'Save this trip', 'keep the plan', 'save it' (but not 'save it to my calendar')."""
    return bool(SAVE_RE.match(message)) and len(message.split()) <= 8


def unsaved_plan(state: dict) -> dict | None:
    plan = (state.get("memory") or {}).get("last_travel_plan")
    return plan if plan and not plan.get("trip_id") else None


async def save_trip(plan: dict, state: dict) -> AgentResult:
    """Save a planned trip to the register, then add it to Google Calendar (asking first if it clashes)."""
    from veltrix_copilot.agents import calendar_agent      # here: calendar_agent imports this module

    trip_id = await trips.try_call(trips.add, plan, state.get("session_id"))
    if not trip_id:
        return AgentResult(text="I couldn't save the trip: the database isn't reachable. "
                                "Is Postgres running? (docker compose up -d db)")
    plan = {**plan, "trip_id": trip_id}
    memory = {**(state.get("memory") or {}), "last_travel_plan": None}   # drop the unsaved copy from chat_trips
    updates = {"last_travel_plan": plan, "chat_trips": followups.remember_trip(memory, plan), **followups.used(plan)}
    text, actions = f"**Saved Trip #{trip_id}** ({followups.plan_label(plan)}).", [f"Saved Trip #{trip_id}"]
    try:
        if found := await calendar_agent.clashes(plan):
            return AgentResult(text=f"{text}\n\n{calendar_agent.clash_text(plan, found)}", actions=actions,
                               memory_updates={**updates, "pending": followups.pending_question(
                                   plan, "calendar", {"force": True})})
        cal_text, cal_actions, _ = await calendar_agent.add_trip(plan)
        return AgentResult(text=f"{text}\n\n{cal_text}", actions=actions + cal_actions, memory_updates=updates)
    except ToolError as e:
        return AgentResult(text=f"{text}\n\nI couldn't add it to Google Calendar: {e}", actions=actions,
                           memory_updates=updates)


async def run(message: str, state: dict, today: date | None = None) -> AgentResult:
    today = today or date.today()
    conf = state.get("confirmation") or {}
    if conf.get("action", {}).get("save") or is_save_request(message):
        answer, plan = (await followups.confirmed_plan(state) if conf else
                        ("yes", unsaved_plan(state) or (state.get("memory") or {}).get("last_travel_plan")))
        if answer == "no":
            return AgentResult(text="OK, I didn't save that trip. Ask me to plan another one any time.",
                               memory_updates={"last_travel_plan": None})
        if not plan:
            return AgentResult(text="There's no unsaved trip plan in this chat. Plan one first, e.g. "
                                    "*Plan a 2-day trip to London next month under $1500.*")
        if plan.get("trip_id"):
            return AgentResult(text=f"Trip #{plan['trip_id']} is already saved.", memory_updates=followups.used(plan))
        return await save_trip(plan, state)

    fields = await extract_trip(message, today)
    if not fields.get("destination"):
        return AgentResult(text="Where would you like to travel? For example: *Plan a 2-day trip to London next month under $1500.*")
    try:
        plan = await build_plan(fields, today, message)
    except ToolError as e:
        return AgentResult(text=f"I couldn't plan that trip: {e}")

    plan["itinerary"] = await write_itinerary(plan)
    # The plan is shown first and saved only when the user says yes (or asks for the calendar in the same
    # message: the calendar agent then saves it). Until then it lives in this chat's memory only.
    updates = {"last_travel_plan": plan, **followups.used(plan)}
    if "calendar" in (state.get("plan") or []):
        return AgentResult(text=render_plan(plan), memory_updates=updates)
    return AgentResult(text=f"{render_plan(plan)}\n\n{SAVE_QUESTION}",
                       memory_updates={**updates, "pending": followups.pending_question(plan, "travel", {"save": True})})
