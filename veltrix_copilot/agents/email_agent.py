"""Email agent: drafts only, always to MANAGER_EMAIL. The model writes the wording; trip facts come from code."""
from __future__ import annotations

import re

from veltrix_copilot.core import llm
from veltrix_copilot.travel import policy_checks, travel_rules, trips
from veltrix_copilot.agents import plans
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.agents.travel_agent import plan_summary_text
from veltrix_copilot.core.config import settings
from veltrix_copilot.tools.mcp_client import ToolError, call_tool
from veltrix_copilot.core.schemas import EmailDraft

EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MONEY_LINE = re.compile(r"[$€£₹]")

TRAVEL_EMAIL_PROMPT = """Write an email to the user's manager about a business trip, as JSON.
The trip details (flights, hotel, costs) are inserted automatically between your opening and closing,
so do NOT repeat any prices, flight numbers or dates.
Trip: {origin} to {destination}, {nights} night(s). Within budget: {within}.
User's request: {request}"""

GENERIC_EMAIL_PROMPT = """Write a short, professional email to the user's manager, as JSON, based on the user's request.
Keep it under 120 words. Do not invent facts the user did not give."""


def _clean(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _MONEY_LINE.search(ln)).strip()


def assemble_body(opening: str, middle: str, closing: str) -> str:
    parts = ["Hi,", opening.strip(), middle.strip(), closing.strip(), f"Best regards,\n{settings.user_name}"]
    return "\n\n".join(p for p in parts if p)


async def travel_email(plan: dict, message: str) -> tuple[str, str]:
    within = {True: "yes", False: "no"}.get(plan.get("within_budget"), "no budget given")
    try:
        draft = await llm.astructured(EmailDraft, [{"role": "user", "content": TRAVEL_EMAIL_PROMPT.format(
            origin=plan["origin"], destination=plan["destination"], nights=plan["nights"], within=within, request=message)}])
    except llm.LLMUnavailable:
        draft = None                                   # the template below still gives a complete draft
    subject = (draft and draft.subject) or f"Travel plan for approval: {plan['destination']} ({plan['start']})"
    opening = (draft and _clean(draft.opening)) or f"I'd like your approval for a business trip to {plan['destination']}. The plan is below."
    closing = (draft and _clean(draft.closing)) or "Could you please review and approve it?"
    if policy_checks.needs_dept_head(plan):
        closing += (f" As the estimated total is above ${travel_rules.DEPT_HEAD_APPROVAL_OVER_USD:,}, "
                    "it also needs the department head's approval under the travel policy (POL-FIN-001).")
    return subject, assemble_body(opening, plan_summary_text(plan), closing)


async def save_draft(subject: str, body: str, trip_id: int | None = None) -> tuple[str, list[str]]:
    """Save a draft to the manager (never sent) and, for a trip, record it. Returns (text, actions)."""
    to = settings.manager_email
    saved = await call_tool("email", "draft_email", {"to": to, "subject": subject[:200], "body": body})
    if trip_id:
        await trips.try_call(trips.mark_email, trip_id, saved["draft_id"])
    quoted = "\n".join(f"> {ln}" if ln else ">" for ln in body.splitlines())
    return (f"**Email draft saved (not sent)**\n\n**To:** {to}  \n**Subject:** {subject}\n\n{quoted}",
            [f"Drafted email to {to} (not sent) · draft {saved['draft_id'][:8]}"])


async def draft_trip_update(plan: dict, change: str) -> tuple[str, list[str]]:
    body = assemble_body(f"My trip plan has changed ({change}). The updated plan is below.",
                         plan_summary_text(plan), "Could you please review and approve the updated plan?")
    return await save_draft(f"Updated travel plan: {plan['destination']} ({plan['start']})", body, plan.get("trip_id"))


async def draft_trip_cancellation(plan: dict) -> tuple[str, list[str]]:
    body = assemble_body(f"Please disregard my earlier request: the trip {plans.plan_label(plan)} is cancelled.", "", "Thank you.")
    return await save_draft(f"Trip cancelled: {plan['destination']} ({plan['start']})", body)


async def run(message: str, state: dict) -> AgentResult:
    to = settings.manager_email
    answer, plan = await plans.confirmed_plan(state)
    if answer == "no":
        return AgentResult(text="OK. What should the email to your manager say?")
    if answer == "yes" and not plan:
        return AgentResult(text="I couldn't find that trip any more. Which trip should the email be about?")
    ref = plans.Ref.PLAN
    if answer is None:
        decision = await plans.resolve(message, state, "email")
        if ask := plans.question(decision, "email"):
            return ask
        ref, plan = decision.ref, decision.plan
    if ref is plans.Ref.PLAN:
        if plan.get("cancelled"):
            return AgentResult(text=f"**{plans.plan_label(plan)}** was cancelled, so I didn't draft an email for it.")
        subject, body = await travel_email(plan, message)
    elif ref is plans.Ref.NONE and plans.mentions_trip(message):
        return AgentResult(text="I don't have a saved trip yet. Ask me to plan one first, "
                                "e.g. *Plan a 2-day trip to London next month under $1500.*")
    else:
        draft = await llm.astructured(EmailDraft, [{"role": "system", "content": GENERIC_EMAIL_PROMPT},
                                                   {"role": "user", "content": message}])
        if not draft:
            return AgentResult(text="I couldn't draft that email. Could you rephrase what it should say?")
        subject, body = draft.subject, assemble_body(draft.opening, "", draft.closing)
    try:
        text, actions = await save_draft(subject, body, plan.get("trip_id") if ref is plans.Ref.PLAN else None)
    except ToolError as e:
        return AgentResult(text=f"I couldn't save the draft: {e}")
    if any(a.lower() != to.lower() for a in EMAIL_ADDR_RE.findall(message)):
        text += f"\n\n_Note: in this prototype drafts are only addressed to your manager ({to})._"
    return AgentResult(text=text, actions=actions, memory_updates=plans.used(plan) if ref is plans.Ref.PLAN else {})
