"""Offline ingestion: data/docs (and its sub-folders) -> chunks -> embeddings -> pgvector.
    uv run -m veltrix_copilot.knowledge.ingest [--reset]   (--reset recreates the table, e.g. after changing EMBED_DIM)"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

from veltrix_copilot.knowledge import db
from veltrix_copilot.core import llm
from veltrix_copilot.core.config import settings
from veltrix_copilot.knowledge.loaders import load_segments, supported


def chunk_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
                                              separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""])
    return [c.strip() for c in splitter.split_text(text) if c.strip()]


def default_embed(texts: list[str]) -> list[list[float]]:
    return llm.embed(texts, kind="document")


def ingest_file(conn, path: Path, embed_fn=None, name: str | None = None) -> int:
    """(Re)index one document under `name` (default: its path inside data/docs), section by section.
    Returns its chunk count."""
    name = name or doc_name(path)
    pieces, metas, heads = [], [], []
    for text, where in load_segments(path):
        for piece in chunk_text(text):
            pieces.append(piece)
            metas.append({"source": name, **{k: v for k, v in where.items() if v}})
            heads.append(name + (f" > {where['section']}" if where.get("section") else ""))
    if not pieces:
        db.delete_document(conn, name)
        return 0
    # The document path and section in front of each chunk help short chunks (a table row, a list item) match.
    vectors = (embed_fn or default_embed)([f"{h}: {p}" for h, p in zip(heads, pieces)])
    db.replace_document(conn, name, pieces, vectors, metas)
    return len(pieces)


def doc_name(path: Path) -> str:
    """A document's name = its path inside data/docs, e.g. 'policies/pol-fin-001-travel-policy.md'."""
    return path.resolve().relative_to(settings.docs_dir.resolve()).as_posix()


def document_files() -> list[Path]:
    return sorted(p for p in settings.docs_dir.rglob("*") if supported(p)
                  and not any(part.startswith(".") for part in p.relative_to(settings.docs_dir).parts))


def ingest(reset: bool = False, embed_fn=None) -> dict[str, int]:
    summary: dict[str, int] = {}
    with db.connect() as conn:
        db.init_schema(conn, reset=reset)
        files = document_files()
        for i, path in enumerate(files, 1):
            t0 = time.perf_counter()
            if n := ingest_file(conn, path, embed_fn):
                summary[doc_name(path)] = n
                print(f"  [{i}/{len(files)}] {doc_name(path):<60} {n:>3} chunks  {time.perf_counter() - t0:5.1f}s")
        for gone in set(db.doc_names(conn)) - set(summary):     # files deleted from data/docs
            db.delete_document(conn, gone)
            print(f"  {gone:<32} removed (file no longer in data/docs)")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the chunks table")
    reset = parser.parse_args().reset
    print(f"Ingesting {settings.docs_dir} with {settings.embed_model} ({settings.embed_dim}-d)...")
    t0 = time.perf_counter()
    s = ingest(reset=reset)
    print(f"Done: {len(s)} documents, {sum(s.values())} chunks in {time.perf_counter() - t0:.1f}s")
