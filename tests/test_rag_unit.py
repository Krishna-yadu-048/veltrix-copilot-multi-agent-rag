from veltrix_copilot.agents import rag_agent
from veltrix_copilot.agents.rag_agent import build_context, match_document, validate_citations

L = "policies/pol-hr-001-leave-policy.md"
CHUNKS = [
    {"doc_name": "policies/pol-hr-001-leave-policy.md", "chunk_index": 0, "content": "24 days of annual leave", "distance": 0.2},
    {"doc_name": "policies/pol-hr-001-leave-policy.md", "chunk_index": 1, "content": "12 sick days", "distance": 0.3},
]


def test_invented_citations_are_removed():
    text, cited = validate_citations("You get 24 days [policies/pol-hr-001-leave-policy.md#0] [fake.md#3].", CHUNKS)
    assert "[fake.md#3]" not in text
    assert "[policies/pol-hr-001-leave-policy.md#0]" in text
    assert [c["chunk_index"] for c in cited] == [0]


def test_context_wraps_documents_as_data_and_respects_budget():
    ctx = build_context(CHUNKS, max_chars=10_000)
    assert '<document id="policies/pol-hr-001-leave-policy.md#0">' in ctx
    assert len(build_context(CHUNKS, max_chars=90)) < len(ctx)


def test_match_document_by_name():
    names = ["policies/pol-hr-001-leave-policy.md", "project_x_design.md", "travel_policy.md"]
    assert match_document("Summarize the project X design document.", names) == "project_x_design.md"
    assert match_document("Summarise the 2024 leave policy", names) == "policies/pol-hr-001-leave-policy.md"
    assert match_document("Summarize everything", names) is None


def test_relevance_gate_refuses_without_calling_the_model(monkeypatch, fake_llm):
    monkeypatch.setattr(rag_agent, "retrieve", lambda q, k=None, exclude=None: [{**CHUNKS[0], "distance": 0.99}])
    monkeypatch.setattr(rag_agent, "load_named_document", lambda q, skip=None: [])
    res = rag_agent.answer("What is the CEO's salary?")
    assert res.text == rag_agent.NOT_FOUND
    assert fake_llm.chat_calls == 0


def test_model_saying_not_in_documents_becomes_refusal(monkeypatch):
    monkeypatch.setattr(rag_agent, "retrieve", lambda q, k=None, exclude=None: CHUNKS)
    monkeypatch.setattr(rag_agent, "load_named_document", lambda q, skip=None: [])
    from veltrix_copilot.core.schemas import RagAnswer
    monkeypatch.setattr(rag_agent.llm, "structured", lambda *a, **k: RagAnswer(found=False, answer=""))
    assert rag_agent.answer("How long is parental leave in 2025?").text == rag_agent.NOT_FOUND
