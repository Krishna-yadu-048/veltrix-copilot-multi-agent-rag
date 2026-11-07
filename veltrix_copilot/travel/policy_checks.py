"""Checks a trip plan against the Veltrix travel policy (POL-FIN-001), quoting the clause each check comes from."""
from __future__ import annotations

import re
from datetime import date

from veltrix_copilot.knowledge import doc_versions
from veltrix_copilot.travel import travel_rules
from veltrix_copilot.core.util import SENTENCES, clean_line, duration

ICON = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}


def policy_document() -> tuple[str, str]:
    """(path, text) of the current travel policy, e.g. policies/pol-fin-001-travel-policy.md; ('', '') if none."""
    from veltrix_copilot.knowledge.ingest import doc_name, document_files
    from veltrix_copilot.knowledge.loaders import load_file

    files = {doc_name(p): p for p in document_files() if {"travel", "policy"} <= set(re.findall(r"[a-z]+", p.stem.lower()))}
    current = [n for n in sorted(files) if n not in doc_versions.superseded(list(files))]
    if not current:
        return "", ""
    try:
        return current[0], load_file(files[current[0]])
    except Exception:
        return current[0], ""


def quote(text: str, pattern: str) -> str:
    """The policy sentence or line matching `pattern`, without Markdown."""
    return next((clean_line(p) for p in SENTENCES.split(text) if re.search(pattern, p, re.I)), "")


def _leg_minutes(leg: dict) -> float | None:
    if isinstance(leg.get("minutes"), (int, float)):
        return float(leg["minutes"])
    return None


def longest_flight(plan: dict) -> tuple[float, str]:
    """(minutes, 'COK → LHR') of the longest single flight in the trip."""
    best, label = 0.0, ""
    for f in (plan["outbound"], plan["return"]):
        legs = f.get("legs") or []
        for leg in legs:
            m = _leg_minutes(leg)
            if m and m > best:
                best, label = m, f"{leg.get('from', '')} → {leg.get('to', '')}"
        if not legs or all(_leg_minutes(leg) is None for leg in legs):     # older plans / no leg data
            m = float(f.get("duration_hours") or 0) * 60
            if m > best and not f.get("stops"):
                best, label = m, f"{f['origin']} → {f['destination']}"
    return best, label


def check(plan: dict, today: date | None = None) -> list[dict]:
    """The plan against POL-FIN-001: approval level, booking notice, class of travel, hotel cap, regions covered."""
    today = today or date.fromisoformat(plan.get("searched_on") or date.today().isoformat())
    doc, text = policy_document()
    out: list[dict] = []
    r = travel_rules

    def add(status: str, message: str, pattern: str) -> None:
        out.append({"status": status, "text": message, "doc": doc, "rule": quote(text, pattern) if text else ""})

    total, limit = plan["costs"]["total"], r.DEPT_HEAD_APPROVAL_OVER_USD
    if total > limit:
        add("warn", f"Over ${limit:,} (${total:,.0f}): needs department head approval", r"department head approval")
    else:
        add("ok", f"Under ${limit:,}: no department head approval needed", r"department head approval")

    domestic = r.is_domestic(plan["origin"], plan["destination"])
    need = r.DOMESTIC_NOTICE_DAYS if domestic else r.INTERNATIONAL_NOTICE_DAYS
    kind = "domestic" if domestic else "international"
    ahead = (date.fromisoformat(plan["start"]) - today).days
    if ahead >= need:
        add("ok", f"Booked {ahead} days ahead (at least {need} for {kind} trips)", r"in advance")
    else:
        add("warn", f"Only {ahead} day{'s' if ahead != 1 else ''} ahead: {kind} trips must be booked at least "
                    f"{need} days in advance through TravelDesk", r"in advance")

    minutes, label = longest_flight(plan)
    hours = r.PREMIUM_ECONOMY_MIN_HOURS
    if domestic:
        add("ok", "Domestic trip: economy class", r"economy class")
    elif minutes > hours * 60:
        add("info", f"Longest flight {duration(minutes)} ({label}): premium economy is allowed; prices shown are "
                    "economy; business class needs CFO approval", r"premium economy")
    elif minutes:
        add("ok", f"Every flight is {hours} hours or less: economy class", r"economy class")

    lim = plan.get("policy") or r.limits(plan["destination"])
    h, cap = plan.get("hotel"), lim["hotel_cap_usd"]
    if not lim["covered"]:
        land = r.country(plan["destination"]) or plan["destination"]
        land = f"the {land}" if land.startswith("United") else land
        add("warn", f"The policy sets no hotel cap or per diem for {land}: the planner used the US amounts "
                    f"({lim['hotel_cap']}/night, {lim['per_diem']}/day) as a guide. Ask the policy owner before booking",
            r"Hotel caps per night")
    elif h:
        verdict = "within" if h["price_per_night"] <= cap else "over"
        add("ok" if verdict == "within" else "warn",
            f"Hotel ${h['price_per_night']}/night is {verdict} the {lim['hotel_cap']} cap for {lim['label']} (≈ ${cap})",
            r"Hotel caps per night")
    return out


def needs_dept_head(plan: dict) -> bool:
    return plan["costs"]["total"] > travel_rules.DEPT_HEAD_APPROVAL_OVER_USD


def render_markdown(checks: list[dict]) -> str:
    if not checks:
        return ""
    doc = next((c["doc"] for c in checks if c.get("doc")), "")
    lines = ["**Policy checks**" + (f" · from {doc}" if doc else "")]
    for c in checks:
        lines.append(f"- {ICON[c['status']]} {c['text']}")
        if c.get("rule"):
            rule = c["rule"].replace("[", "(").replace("]", ")")
            lines.append(f"    - :gray[“{rule}”]")
    return "\n".join(lines)


LABEL = {"ok": "OK", "warn": "CHECK", "info": "NOTE"}


def render_text(checks: list[dict]) -> list[str]:
    """Plain lines for the email: 'OK: Booked 16 days ahead (at least 14)'."""
    return [f"{LABEL[c['status']]}: {c['text']}" for c in checks]
