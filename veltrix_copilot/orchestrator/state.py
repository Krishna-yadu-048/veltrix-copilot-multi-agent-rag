from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AssistantState(TypedDict, total=False):
    messages: Annotated[list, add_messages]  # chat history (kept for the session, NOT sent to agents)
    plan: list[str]                          # agents still to run this turn
    results: dict[str, str]                  # this turn's agent outputs (reset each turn)
    sources: list[dict]                      # RAG citations (reset each turn)
    actions: list[str]                       # side effects performed (reset each turn)
    timings: dict[str, float]                # seconds per node (reset each turn)
    session_id: str                          # the chat this turn belongs to
    memory: dict                             # survives turns: last_travel_plan, chat_trips (this chat's trips),
                                             #   last_turn {agents, summary, produced_plan, trip_key},
                                             #   pending (question asked), turn_trip (trip used this turn)
    produced: list[str]                      # what this turn created, e.g. ["travel_plan"] (reset each turn)
    confirmation: dict                       # this turn answers our "Do you mean…?" / "Which trip?" question:
                                             #   {agent, answer: "yes"|"no", chosen: {key, trip_id, …}} (reset each turn)
    final_answer: str
