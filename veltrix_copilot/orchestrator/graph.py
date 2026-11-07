"""LangGraph orchestrator: router -> dispatch -> agent -> dispatch ... -> merge."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from veltrix_copilot.core import llm
from veltrix_copilot.agents import calendar_agent, email_agent, rag_agent, travel_agent, trips_agent
from veltrix_copilot.agents.plans import choice_reply, option_reply
from veltrix_copilot.agents.base import AgentResult
from veltrix_copilot.tools.mcp_client import ToolUnavailable
from veltrix_copilot.orchestrator.router import confirmation_reply, route
from veltrix_copilot.orchestrator.state import AssistantState

log = logging.getLogger(__name__)

AgentFn = Callable[[str, dict], Awaitable[AgentResult]]
AGENTS: dict[str, AgentFn] = {
    "rag": rag_agent.run,
    "travel": travel_agent.run,
    "email": email_agent.run,
    "calendar": calendar_agent.run,
    "trips": trips_agent.run,
}
HEADINGS = {"rag": "From the company documents", "travel": "Trip plan", "email": "Email draft", "calendar": "Calendar",
            "trips": "My trips"}


def last_user_message(state: AssistantState) -> str:
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""


async def router_node(state: AssistantState) -> dict:
    t0 = time.perf_counter()
    message = last_user_message(state)
    memory = dict(state.get("memory") or {})
    memory.pop("turn_trip", None)  # set by agents during a turn; never carried over
    # A question we asked last turn ("Do you mean Trip #7?" / "Which trip?") is only valid for one turn.
    confirmation = answer_to(memory.pop("pending", None), message)
    plan = [confirmation["agent"]] if confirmation else route(message)
    log.info("route=%s%s", plan, f" (answer '{confirmation['answer']}' to a question)" if confirmation else "")
    return {
        "plan": plan, "results": {}, "sources": [], "actions": [], "final_answer": "",
        "timings": {"router": round(time.perf_counter() - t0, 3)},
        "memory": memory, "produced": [], "confirmation": confirmation,
    }


def answer_to(pending: dict | None, message: str) -> dict:
    """Is this message an answer to the question we asked last turn? Returns the confirmation or {}."""
    if not pending:
        return {}
    choices = pending.get("choices") or []
    base = {"agent": pending["agent"], "action": pending.get("action") or {}}
    reply = confirmation_reply(message)
    if reply == "no":
        return {**base, "answer": "no"}
    if pending.get("kind") == "options":                       # "2", "1 and 5": pick flight options
        picks = option_reply(message, pending.get("count", 0))
        return {**base, "answer": "yes", "picks": picks, "chosen": choices[0] if choices else {}} if picks else {}
    if pending.get("kind") == "choose":
        idx = choice_reply(message, choices)
        if idx is None and reply == "yes" and len(choices) == 1:
            idx = 0
        return {**base, "answer": "yes", "chosen": choices[idx]} if idx is not None else {}
    if reply == "yes":
        return {**base, "answer": "yes", "chosen": choices[0] if choices else {}}
    return {}


def next_step(state: AssistantState) -> str:
    plan = state.get("plan") or []
    return plan[0] if plan else "merge"


def make_agent_node(name: str):
    async def node(state: AssistantState) -> dict:
        t0 = time.perf_counter()
        message = last_user_message(state)  # agents get the current message only, never full history
        try:
            res = await AGENTS[name](message, state)
        except llm.LLMUnavailable as e:
            log.error("%s agent: LLM unavailable: %s", name, e)
            res = AgentResult(text="The local language model isn't responding. Check that Ollama is running "
                                   "(`ollama ps`) and the model is pulled.")
        except ToolUnavailable as e:
            log.error("%s agent: tool unavailable: %s", name, e)
            res = AgentResult(text=f"The {name} tool server isn't running. Start everything with `uv run scripts/dev.py`.")
        except Exception:  # never leak a stack trace to the user
            log.exception("%s agent failed", name)
            res = AgentResult(text=f"Sorry, the {name} agent hit an unexpected error. Check the API logs.")
        memory = {**(state.get("memory") or {}), **res.memory_updates}
        produced = list(state.get("produced") or [])
        if "last_travel_plan" in res.memory_updates:
            produced.append("travel_plan")
        return {
            "produced": produced,
            "plan": (state.get("plan") or [])[1:],
            "results": {**(state.get("results") or {}), name: res.text},
            "sources": (state.get("sources") or []) + res.sources,
            "actions": (state.get("actions") or []) + res.actions,
            "memory": memory,
            "timings": {**(state.get("timings") or {}), name: round(time.perf_counter() - t0, 3)},
        }

    node.__name__ = f"{name}_node"
    return node


def merge_node(state: AssistantState) -> dict:
    """Template merge, no LLM call: never distorts agent outputs and saves a model call."""
    results = state.get("results") or {}
    if len(results) == 1:
        answer = next(iter(results.values()))
    elif results:
        answer = "\n\n---\n\n".join(f"### {HEADINGS.get(k, k.title())}\n\n{v}" for k, v in results.items())
    else:
        answer = "I'm not sure how to help with that."
    memory = dict(state.get("memory") or {})
    last_turn = turn_summary(state)
    last_turn["trip_key"] = memory.pop("turn_trip", None)   # the trip this turn worked on, if any
    memory["last_turn"] = last_turn
    return {"final_answer": answer, "messages": [AIMessage(content=answer)], "memory": memory}


def turn_summary(state: AssistantState) -> dict:
    """What this turn did, in one line (built in code). The next turn uses it to resolve
    follow-ups like "add it to my calendar"; the LLM sees only this line, never the history."""
    memory = state.get("memory") or {}
    agents = list(state.get("results") or {})
    produced_plan = "travel_plan" in (state.get("produced") or [])
    bits = []
    if produced_plan and memory.get("last_travel_plan"):
        p = memory["last_travel_plan"]
        bits.append(f"planned a trip {p['origin']} to {p['destination']} ({p['start']} to {p['end']})")
    if "email" in agents:
        bits.append("drafted an email" if any("Drafted email" in a for a in state.get("actions") or []) else "talked about an email")
    if "calendar" in agents:
        bits.append("added calendar events" if any("Added" in a for a in state.get("actions") or []) else "talked about the calendar")
    if "rag" in agents:
        docs = sorted({s["doc_name"] for s in state.get("sources") or []})
        bits.append(f"answered a question from {', '.join(docs)}" if docs else "answered a question")
    if "travel" in agents and not produced_plan:
        bits.append("asked for trip details (no plan made)")
    return {"agents": agents, "produced_plan": produced_plan, "summary": "; ".join(bits) or "nothing"}


def build_graph(checkpointer=None):
    g = StateGraph(AssistantState)
    g.add_node("router", router_node)
    g.add_node("dispatch", lambda state: {})
    for name in AGENTS:
        g.add_node(name, make_agent_node(name))
    g.add_node("merge", merge_node)

    g.add_edge(START, "router")
    g.add_edge("router", "dispatch")
    g.add_conditional_edges("dispatch", next_step, {**{n: n for n in AGENTS}, "merge": "merge"})
    for name in AGENTS:
        g.add_edge(name, "dispatch")
    g.add_edge("merge", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


async def run_turn(graph, session_id: str, message: str, memory: dict | None = None) -> dict:
    """Run one turn. If `memory` is given (loaded from the chat store), it replaces the
    checkpointer's copy, so plans survive API restarts."""
    inputs: dict = {"messages": [HumanMessage(content=message)], "session_id": session_id}
    if memory is not None:
        inputs["memory"] = memory
    return await graph.ainvoke(inputs, config={"configurable": {"thread_id": session_id}, "recursion_limit": 25})
