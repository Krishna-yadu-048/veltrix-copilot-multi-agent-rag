"""Test set-up: import the app from the project folder, and a fake chat model (no Ollama needed)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from veltrix_copilot.core import llm  # noqa: E402
from veltrix_copilot.core.schemas import RagAnswer  # noqa: E402


class FakeLLM:
    """Answers like the real model would, citing the first document plus one invented citation."""
    def __init__(self):
        self.chat_calls = 0

    def structured(self, schema, messages, **kw):
        self.chat_calls += 1
        ids = re.findall(r'<document id="([^"]+)"', "\n".join(m["content"] for m in messages))
        if schema is RagAnswer and ids:
            return RagAnswer(found=True, answer=f"Here is the answer [{ids[0]}] [made_up.md#7].")
        return None


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm, "structured", fake.structured)
    return fake
