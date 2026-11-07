"""What every agent returns to the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentResult:
    text: str
    sources: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    memory_updates: dict = field(default_factory=dict)
