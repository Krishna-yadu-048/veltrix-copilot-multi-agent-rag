"""Which saved trip does a follow-up mean? Rules first (trip number, destination/month, the trip in play),
one small optional LLM call when unclear, otherwise ask. Used by the email, calendar and trips agents."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from veltrix_copilot.core import llm
from veltrix_copilot.travel import trips
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.core.config import settings
from veltrix_copilot.core.dates import MONTHS, fmt_day, has_date_hint
from veltrix_copilot.core.schemas import FollowUpDecision

log = logging.getLogger(__name__)

# Words that can only mean a trip.
TRIP_WORDS_RE = re.compile(r"\b(travel|trip|itinerary|plan|flights?|hotels?|journey|booking)\b", re.I)
# Words that point back at "whatever we just talked about".
POINTER_RE = re.compile(r"\b(this|that|these|those|it|them|events?|everything|all of (it|them)|the details)\b", re.I)
# Email requests that bring their own content ("saying I'll be late", "about the budget meeting").
EMAIL_OWN_CONTENT_RE = re.compile(
    r"\b(saying|that says|to say|telling|asking|explaining|about|regarding|re:|informing|letting \w+ know)\b", re.I)
# "trip #7", "#7", "trip 7", "trip no. 7", "trip number 7" (but not "trip 3 days").
TRIP_ID_RE = re.compile(r"(?:#\s?(\d{1,6})\b|\btrip\s+(?:no\.?\s*|number\s*)?(\d{1,6})\b(?!\s*-?\s*(?:days?|nights?)))", re.I)
_MONTH_NAMES = {k: v for k, v in MONTHS.items()}
MAX_CHOICES = 5
CHAT_TRIPS_KEPT = 10


class Ref(str, Enum):
    PLAN = "plan"      # use this trip
    NEW = "new"        # the message describes something else
    ASK = "ask"        # one likely trip: ask "Do you mean …?"
    CHOOSE = "choose"  # several possible trips: ask "Which one?" with a numbered list
    NONE = "none"      # no trip to talk about


@dataclass
class Decision:
    ref: Ref
    plan: dict | None = None
    choices: list[dict] = field(default_factory=list)
    note: str = ""


def trip_key(plan: dict) -> str:
    """Stable key: the trip number, or (when the database was down) destination + start date."""
    if plan.get("trip_id"):
        return f"#{plan['trip_id']}"
    return f"{plan['destination'].lower()}:{plan['start']}"


def plan_label(plan: dict) -> str:
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    num = f"Trip #{plan['trip_id']} · " if plan.get("trip_id") else ""
    return f"{num}{plan['origin']} → {plan['destination']}, {fmt_day(start)} – {fmt_day(end)}"


def chat_plans(memory: dict | None) -> list[dict]:
    """Trips planned in this chat, oldest first (includes chats saved before the trip register)."""
    memory = memory or {}
    out: list[dict] = list(memory.get("chat_trips") or [])
    if memory.get("last_travel_plan"):
        out.append(memory["last_travel_plan"])
    seen, unique = set(), []
    for p in out:
        if trip_key(p) not in seen:
            seen.add(trip_key(p))
            unique.append(p)
    return unique


def remember_trip(memory: dict | None, plan: dict) -> list[dict]:
    """The chat's trip list with `plan` added (newest last, capped)."""
    kept = [p for p in chat_plans({"chat_trips": (memory or {}).get("chat_trips")}) if trip_key(p) != trip_key(plan)]
    return (kept + [plan])[-CHAT_TRIPS_KEPT:]


def in_play_key(state: dict) -> str | None:
    """The trip this turn already used (travel then calendar in one message), else the one the previous turn used."""
    memory = state.get("memory") or {}
    return memory.get("turn_trip") or (memory.get("last_turn") or {}).get("trip_key")


async def known_trips(state: dict) -> list[dict]:
    """Every saved trip (newest first). Falls back to this chat's trips if the database is down."""
    rows = await trips.try_call(trips.list_trips, 200)
    if rows is None:
        return list(reversed(chat_plans(state.get("memory"))))
    return [r["plan"] for r in rows]


def mentions_trip(message: str) -> bool:
    return bool(TRIP_WORDS_RE.search(message))


_ONLY_POINTER_RE = re.compile(
    r"^\s*(it|this|that|them|these|those|everything|the (trip|plan|itinerary|travel plan|flights?|details))\b[\s.!?]*$", re.I)


def has_own_content(message: str, agent: str) -> bool:
    """Calendar: the message gives a date. Email: it says what to write ("saying I'll be late"),
    not just "about it" / "about the trip", which point back at the plan."""
    if agent == "calendar":
        return has_date_hint(message)
    if agent != "email":
        return False
    m = EMAIL_OWN_CONTENT_RE.search(message)
    return bool(m) and not _ONLY_POINTER_RE.match(message[m.end():])


def trip_number(message: str) -> int | None:
    m = TRIP_ID_RE.search(message)
    return int(m.group(1) or m.group(2)) if m else None


def months_named(message: str) -> set[int]:
    """Month names in the text. 'may' only counts when written 'May' (it is also a verb)."""
    found = set()
    for name, num in _MONTH_NAMES.items():
        flags = 0 if name in ("may",) else re.I
        pattern = r"\bMay\b" if name == "may" else rf"\b{name}\b"
        if re.search(pattern, message, flags):
            found.add(num)
    return found


def day_named(message: str, months: set[int]) -> int | None:
    """'10 March', 'March 10th' -> 10 (only next to a month name)."""
    if not months:
        return None
    names = "|".join(n for n in _MONTH_NAMES)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{names})\b|\b(?:{names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
                  message, re.I)
    return int(m.group(1) or m.group(2)) if m else None


def matches_dates(plan: dict, months: set[int], day: int | None) -> bool:
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    if day is not None:
        for month in months:
            for year in {start.year, end.year}:
                try:
                    if start <= date(year, month, day) <= end:
                        return True
                except ValueError:
                    pass
        return False
    return start.month in months or end.month in months


def destinations_named(message: str, plans: list[dict]) -> set[str]:
    text = message.lower()
    return {p["destination"].lower() for p in plans if re.search(rf"\b{re.escape(p['destination'].lower())}\b", text)}


def _by_start(plans: list[dict]) -> list[dict]:
    return sorted(plans, key=lambda p: (p["start"], p.get("trip_id") or 0))


def pick(message: str, state: dict, agent: str, all_trips: list[dict]) -> Decision:
    """Rules only, no LLM. `all_trips` = every saved trip (newest first)."""
    memory = state.get("memory") or {}
    chat = chat_plans(memory)
    chat_keys = {trip_key(p) for p in chat}
    everything: dict[str, dict] = {}
    for p in [*all_trips, *reversed(chat)]:                 # register first; chat-only trips (DB was down) too
        everything.setdefault(trip_key(p), p)
    cancelled = {k for k, p in everything.items() if p.get("cancelled")}
    pool = {k: p for k, p in everything.items() if k not in cancelled}   # cancelled trips only by number
    if not everything:
        return Decision(Ref.NONE)

    # 1. A trip number (a cancelled trip is returned too; the agent says it was cancelled)
    if (num := trip_number(message)) is not None:
        if found := everything.get(f"#{num}"):
            return Decision(Ref.PLAN, found)
        return Decision(Ref.CHOOSE, choices=list(pool.values())[:MAX_CHOICES], note=f"I can't find Trip #{num}.")
    if not pool:
        return Decision(Ref.NONE)
    inplay = pool.get(in_play_key(state) or "")
    recent = list(pool.values())[:MAX_CHOICES]
    chat = [p for p in chat if trip_key(p) not in cancelled]

    # 2. Destination and/or month named
    named = destinations_named(message, list(pool.values()))
    months = months_named(message)
    if named or (months and mentions_trip(message)):
        cands = [p for p in pool.values() if not named or p["destination"].lower() in named]
        note = ""
        if months:
            dated = [p for p in cands if matches_dates(p, months, day_named(message, months))]
            if dated:
                cands = dated
            else:
                where = f"{'/'.join(sorted(d.title() for d in named))} " if named else ""
                note = f"I don't have a {where}trip on those dates."
        if len(cands) == 1 and not note:
            return Decision(Ref.PLAN, cands[0])
        if not note:
            if inplay is not None and trip_key(inplay) in {trip_key(p) for p in cands}:
                return Decision(Ref.PLAN, inplay)
            from_chat = [p for p in cands if trip_key(p) in chat_keys]
            if len(from_chat) == 1:
                return Decision(Ref.PLAN, from_chat[0])
        return Decision(Ref.CHOOSE, choices=_by_start(cands)[:MAX_CHOICES], note=note)

    # 3. Nothing named: the trip in play, else this chat's latest trip
    default = inplay or ((pool.get(trip_key(chat[-1])) or chat[-1]) if chat else None)
    about_trip = mentions_trip(message) or bool(POINTER_RE.search(message))
    if default is None:
        # 4. Trips exist, but none in this chat and none in play
        if about_trip and not has_own_content(message, agent):
            if len(pool) == 1:
                return Decision(Ref.ASK, recent[0])
            return Decision(Ref.CHOOSE, choices=_by_start(recent))
        return Decision(Ref.NONE)
    if has_own_content(message, agent):
        return Decision(Ref.ASK if POINTER_RE.search(message) else Ref.NEW, default)
    if about_trip:
        return Decision(Ref.PLAN, default)
    if inplay is not None and default is inplay:
        return Decision(Ref.PLAN, default)
    return Decision(Ref.ASK, default)


def context_line(state: dict) -> str:
    """A ONE-LINE summary of what is in play, built in code (never the chat history)."""
    memory = state.get("memory") or {}
    parts = []
    last = (memory.get("last_turn") or {}).get("summary")
    if last:
        parts.append(f"Previous turn: {last}.")
    chat = chat_plans(memory)
    if chat:
        parts.append(f"Saved trip: {plan_label(chat[-1])}.")
    return " ".join(parts)


RESOLVE_PROMPT = """Decide whether the user's new message is about their saved trip.
{context}
Answer as JSON: {{"about_saved_trip": true|false, "confident": true|false}}.
Set confident=false if the message could reasonably mean something else."""


async def resolve(message: str, state: dict, agent: str) -> Decision:
    """Rules first; if they say ASK and USE_LLM_FOLLOWUPS is on, one small LLM call decides.
    The model only sees one line of context, so the input stays tiny on a 4 GB GPU."""
    decision = pick(message, state, agent, await known_trips(state))
    if decision.ref is not Ref.ASK or not settings.use_llm_followups:
        return decision
    try:
        answer = await llm.astructured(FollowUpDecision, [
            {"role": "system", "content": RESOLVE_PROMPT.format(context=context_line(state))},
            {"role": "user", "content": message}], num_predict=60)
    except llm.LLMUnavailable:
        return decision
    if answer and answer.confident:
        log.info("follow-up resolved by LLM: about_saved_trip=%s", answer.about_saved_trip)
        return Decision(Ref.PLAN if answer.about_saved_trip else Ref.NEW, decision.plan)
    return decision


def ask_text(plan: dict, agent: str) -> str:
    if agent == "trips":
        return f"Do you mean **{plan_label(plan)}**? Reply **yes** to go ahead."
    if agent == "calendar":
        return (f"Do you mean **{plan_label(plan)}**? Reply **yes** to add its flights and hotel "
                "to the calendar, or tell me the date and time of a different event.")
    return (f"Do you mean an email about **{plan_label(plan)}**? Reply **yes** to draft it, "
            "or tell me what the email should say.")


def choose_text(choices: list[dict], agent: str, note: str = "") -> str:
    question = {"calendar": "Which trip should I add to the calendar?",
                "email": "Which trip should I put in the email?"}.get(agent, "Which trip do you mean?")
    lines = [f"{i}. {plan_label(p)}" for i, p in enumerate(choices, 1)]
    head = (note + " " if note else "") + question
    return f"{head}\n\n" + "\n".join(lines) + "\n\nReply with the number (e.g. **1**), the trip number (e.g. **#" \
        f"{choices[0].get('trip_id') or 1}**) or the month."


def question(decision: Decision, agent: str, action: dict | None = None) -> AgentResult | None:
    """The reply asking "Which trip?" (CHOOSE) or "Do you mean …?" (ASK), with the question saved; else None."""
    if decision.ref is Ref.CHOOSE:
        return AgentResult(text=choose_text(decision.choices, agent, decision.note),
                           memory_updates={"pending": pending_choice(decision.choices, agent, action)})
    if decision.ref is Ref.ASK:
        return AgentResult(text=ask_text(decision.plan, agent),
                           memory_updates={"pending": pending_question(decision.plan, agent, action)})
    return None


def _choice_info(p: dict) -> dict:
    return {"key": trip_key(p), "trip_id": p.get("trip_id"), "destination": p["destination"],
            "start": p["start"], "end": p["end"]}


def pending_question(plan: dict, agent: str, action: dict | None = None) -> dict:
    return {"agent": agent, "choices": [_choice_info(plan)], "kind": "confirm", "action": action or {}}


def pending_choice(choices: list[dict], agent: str, action: dict | None = None) -> dict:
    """`action` is what to do once the trip is chosen, e.g. {"do": "nights", "value": 3} for the trips agent."""
    return {"agent": agent, "choices": [_choice_info(p) for p in choices], "kind": "choose", "action": action or {}}


def pending_options(plan: dict, agent: str, count: int, action: dict) -> dict:
    """A numbered list of alternatives (e.g. flights); the reply '2' or '2 and 5' picks them."""
    return {"agent": agent, "choices": [_choice_info(plan)], "kind": "options", "count": count, "action": action}


def option_reply(message: str, count: int) -> list[int]:
    """Option numbers in a short reply: '2', 'option 2', 'use 1 and 5', '2, 4' -> [2], [1, 5], [2, 4]."""
    text = message.strip().lower()
    if not count or len(text.split()) > 10:
        return []
    for word, n in _ORDINALS.items():
        text = re.sub(rf"\b{word}\b", str(n), text)
    if re.search(r"[a-z]{3,}", re.sub(r"\b(option|options|number|no|use|pick|take|choose|switch|to|and|the|one|"
                                       r"please|flight|flights|go|with|ok|okay)\b", "", text)):
        return []                                                   # a different request, not an answer
    nums = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)]
    picks = [n for n in dict.fromkeys(nums) if 1 <= n <= count]
    return picks if picks and len(picks) == len(set(nums)) else []


_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
             "fifth": 5, "5th": 5}


def choice_reply(message: str, choices: list[dict]) -> int | None:
    """Which option does a short reply pick? Returns a 0-based index, or None if it isn't an answer."""
    text = message.strip()
    if not choices or len(text.split()) > 8:
        return None
    if (num := trip_number(text)) is not None:                       # "#7", "trip 7"
        for i, c in enumerate(choices):
            if c.get("trip_id") == num:
                return i
        return None
    if m := re.fullmatch(r"\s*(?:option|number|no\.?)?\s*(\d{1,2})\s*[.!]?\s*", text, re.I):   # "2"
        n = int(m.group(1))
        if 1 <= n <= len(choices):
            return n - 1
        for i, c in enumerate(choices):                              # a bare trip number
            if c.get("trip_id") == n:
                return i
        return None
    low = text.lower()
    for word, n in _ORDINALS.items():
        if re.search(rf"\b{word}\b", low) and n <= len(choices):
            return n - 1
    if re.search(r"\b(last|latest)\b", low):
        return len(choices) - 1
    hits = list(range(len(choices)))
    months = months_named(text)
    if months:
        hits = [i for i in hits if matches_dates(choices[i], months, day_named(text, months))]
    dests = {c["destination"].lower() for c in choices if re.search(rf"\b{re.escape(c['destination'].lower())}\b", low)}
    if dests:
        hits = [i for i in hits if choices[i]["destination"].lower() in dests]
    if (months or dests) and len(hits) == 1:
        return hits[0]
    return None


async def load_trip(info: dict, state: dict) -> dict | None:
    """The full plan for a remembered choice: fresh from the trip register (so edits and cancellations made
    elsewhere count), else this chat's copy (database down, or a trip saved without a number)."""
    if info.get("trip_id"):
        row = await trips.try_call(trips.get, int(info["trip_id"]))
        if row:
            return row["plan"]
    for p in chat_plans(state.get("memory")):
        if trip_key(p) == info.get("key"):
            return p
    return None


async def confirmed_plan(state: dict) -> tuple[str | None, dict | None]:
    """If this turn answers our question: ('yes'|'no', the chosen trip)."""
    conf = state.get("confirmation") or {}
    if not conf:
        return None, None
    if conf.get("answer") != "yes":
        return conf.get("answer"), None
    return "yes", await load_trip(conf.get("chosen") or {}, state)


def used(plan: dict) -> dict:
    """memory_updates for an agent that worked on this trip: it is now the trip in play."""
    return {"turn_trip": trip_key(plan)}
