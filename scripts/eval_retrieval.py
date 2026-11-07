"""Measure how well document search finds the right files, using the Veltrix question set.

    uv run scripts/eval_retrieval.py            # retrieval hit-rate over all 488 questions (embeddings only)
    uv run scripts/eval_retrieval.py --k 8      # top-8 instead of RAG_TOP_K
    uv run scripts/eval_retrieval.py --limit 50 --answers   # also ask the model and check the answers

A question counts as a hit when at least one of its source files is among the retrieved chunks or the
documents found in code (knowledge/lookup.py: collection indexes, fiscal quarters, IDs).
Needs Postgres with the documents ingested and Ollama running."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from veltrix_copilot.agents import rag_agent  # noqa: E402
from veltrix_copilot.core.config import settings  # noqa: E402


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9$€₹.]+", text.lower())) - {"the", "a", "an", "of", "and", "in", "is", "to"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=settings.rag_top_k)
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions")
    ap.add_argument("--answers", action="store_true", help="also generate answers (slow) and check key words")
    a = ap.parse_args()
    rows = [json.loads(line) for line in (ROOT / "evals" / "veltrix_qa.jsonl").read_text(encoding="utf-8").splitlines() if line]
    rows = rows[: a.limit] if a.limit else rows
    hits, total, answered = Counter(), Counter(), Counter()
    t0 = time.perf_counter()
    for i, r in enumerate(rows, 1):
        kind = r["type"]
        if kind == "unanswerable":           # nothing to retrieve: only checkable by asking (should decline)
            if a.answers:
                total[kind] += 1
                hits[kind] += not rag_agent.answer(r["question"]).sources
            continue
        total[kind] += 1
        skip = rag_agent.versions_to_skip(r["question"])
        chunks = rag_agent.retrieve(r["question"], k=a.k, exclude=list(skip))
        got = {c["doc_name"] for c in chunks} | rag_agent.pinned(r["question"], skip).names()   # + found in code
        hits[kind] += bool(got & set(r["sources"]))
        if a.answers:
            reply = rag_agent.answer(r["question"]).text
            key = words(r["answer"])
            answered[kind] += len(key & words(reply)) >= max(1, len(key) // 2)
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} questions...")
    print(f"\nTop-{a.k} retrieval hit-rate ({time.perf_counter() - t0:.0f}s)")
    for kind in sorted(total):
        line = f"  {kind:<26} {hits[kind]:>4}/{total[kind]:<4} {100 * hits[kind] / total[kind]:5.1f}%"
        if a.answers and kind != "unanswerable":
            line += f"   answers ~right: {answered[kind]}/{total[kind]}"
        print(line)
    print(f"  {'all':<26} {sum(hits.values()):>4}/{sum(total.values()):<4} {100 * sum(hits.values()) / sum(total.values()):5.1f}%")


if __name__ == "__main__":
    main()
