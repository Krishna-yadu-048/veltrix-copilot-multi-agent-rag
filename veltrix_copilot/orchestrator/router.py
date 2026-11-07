"""Rule-based router (no LLM): which agents handle a message, in the fixed order travel -> email -> calendar."""
from __future__ import annotations

import re

ORDER = ["travel", "email", "calendar"]

TRAVEL_PATTERNS = [
    r"\b(plan|book|organi[sz]e|arrange|find|need|want|create|make)\b[^.?!]{0,40}\b(trip|travel|flights?|hotels?|itinerary|visit)\b",
    r"\b(trip|travel|fly|flying|go|going|visit)\s+to\s+[a-z]",
    r"\b\d+[\s-]*(day|night)s?\s+(business\s+)?trip\b",
    r"\b(one|two|three|four|five|six|seven)[\s-]*days?\s+(business\s+)?trip\b",
]
EMAIL_PATTERNS = [
    r"\b(draft|write|compose|prepare|send|create)\b[^.?!]{0,40}\b(e-?mail|mail|message|note)\b",
    r"\be-?mail\b[^.?!]{0,30}\b(to|my manager|him|her|them|it)\b",
    r"\be-?mail (my|the) (manager|boss|team)\b",
    # "send it to my manager", "forward the plan to my boss" (no word 'email')
    r"\b(send|forward|share|mail|write|message)\b[^.?!]{0,30}\bto (my |the )?(manager|boss|team|lead)\b",
]
CALENDAR_PATTERNS = [
    r"\b(save|add|put|create|schedule|block|book)\b[^.?!]{0,40}\b(calendar|diary|event|meeting)\b",
    r"\bto (my |the )?calendar\b",
    r"\bschedule (a|an|the)\b",
    # "add it to my schedule", "put these in my agenda"
    r"\b(save|add|put)\b[^.?!]{0,40}\b(schedule|agenda)\b",
]
POLICY_WORDS = re.compile(
    r"\b(polic(y|ies)|allowed|allowance|entitle(d|ment)|rules?|cap|limit|reimburs\w*|per diem|"
    r"how (many|much|long)|what (is|are|'s) (our|the)|handbook|guideline|summar\w*)\b",
    re.I,
)
ACTION_VERBS = re.compile(
    r"\b(plan|book|organi[sz]e|arrange|draft|write|compose|prepare|send|save|add|put|schedule|block|create|make|e-?mail)\b",
    re.I,
)
# "the trip to Mumbai", "my London trip", "trip #7": a saved trip, not a request for a new one.
TRIP_REF_RE = re.compile(
    r"#\s?\d|\btrip\s+(?:no\.?\s*|number\s*)?\d+\b(?!\s*-?\s*(?:days?|nights?))"
    r"|\b(?:the|my|that|this|our)\s+(?:[\w-]+\s+){0,2}(?:trip|travel plan|plan|itinerary)\b", re.I)
NEW_TRIP_RE = re.compile(r"\b(?:plan|book|organi[sz]e|arrange)\s+(?:me\s+|us\s+)?(?:a|an|another)\b", re.I)
DESTINATION_RE = re.compile(r"\b(?:to|in|visit(?:ing)?)\s+(?!my\b|the\b|our\b|him\b|her\b|them\b|it\b)([A-Z][a-zA-Z]+)")


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


YES_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|correct|right|please( do)?|go ahead|do it|that one|confirm(ed)?|"
    r"yes,? please|sounds good|absolutely|y)\b[\s.!]*(please|thanks|thank you)?[\s.!]*$", re.I)
NO_RE = re.compile(r"^\s*(no|nope|nah|not that|neither|cancel|never ?mind|n)\b[\s.!,]*", re.I)


def confirmation_reply(message: str) -> str | None:
    """Is this a short yes/no answer to a question we asked? ('yes', 'no' or None)."""
    if len(message.split()) > 6:
        return None
    if YES_RE.match(message):
        return "yes"
    if NO_RE.match(message):
        return "no"
    return None


# Questions and changes about trips that are already saved -> the trips agent (never a new trip).
TRIPS_PATTERNS = [
    r"\bhow many (?:\w+ )?trips\b",
    r"\b(?:my|our|all|upcoming|future|past|planned|saved|cancell?ed|previous)\s+trips\b",
    r"\b(?:list|show|what|which)\b[^.?!]{0,30}\btrips\b",
    r"\btrips\b[^.?!]{0,30}\b(?:planned|booked|saved|this month|next month|last month)\b",
    r"\b(?:other|alternative|different|more|cheaper|faster|shorter)\s+(?:flights?|flight options)\b",
    r"\bcompare\b[^.?!]{0,30}\bflights?\b",
    r"\b(?:cheaper|less expensive|better|nicer|higher[- ]rated|different|another)\s+hotel\b",
    r"\b(?:make|change|extend|shorten|set|reduce|increase)\b[^.?!]{0,30}\b\d{1,2}\s*(?:nights?|days?)\b",
    r"\bcancel\b[^.?!]{0,30}\b(?:trip|it|this|that|plan|booking|#\s?\d+)\b",
]
TRIPS_POLICY_RE = re.compile(r"\b(allowed|polic(y|ies)|entitle\w*|can i|may i|per year|limit)\b", re.I)


def is_trips_request(text: str) -> bool:
    return _any(TRIPS_PATTERNS, text) and not TRIPS_POLICY_RE.search(text) and not NEW_TRIP_RE.search(text)


SAVE_TRIP_RE = re.compile(r"^\s*(?:please\s+)?(?:save|keep)\s+(?:it|this|that|the|my)?\s*(?:trip|plan|itinerary)?\s*[.!]*\s*$",
                          re.I)


def route(message: str) -> list[str]:
    text = message.strip()
    if SAVE_TRIP_RE.match(text):                     # "save this trip": the plan shown in this chat
        return ["travel"]
    if is_trips_request(text):                       # before the policy rule: "how many trips…"
        return ["trips"]
    if POLICY_WORDS.search(text) and not ACTION_VERBS.search(text):
        return ["rag"]                               # "What's the hotel cap?" is a document question
    matched = {"travel": _any(TRAVEL_PATTERNS, text), "email": _any(EMAIL_PATTERNS, text),
               "calendar": _any(CALENDAR_PATTERNS, text)}
    # travel next to email/calendar with no new destination means the existing plan
    if matched["travel"] and (matched["email"] or matched["calendar"]) and (
            (TRIP_REF_RE.search(text) and not NEW_TRIP_RE.search(text)) or not DESTINATION_RE.search(text)):
        matched["travel"] = False
    return [a for a in ORDER if matched[a]] or ["rag"]
