"""Postgres + pgvector access. Every query is parameterised (no f-string SQL with user input)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg.conninfo
from pgvector import Vector
from pgvector.psycopg import register_vector

from veltrix_copilot.core.config import settings


def unreachable_hint(url: str) -> str:
    """What to check when Postgres doesn't answer (the password is never shown)."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        info = {}
    where = f"{info.get('host', '?')}:{info.get('port', 5432)}, database '{info.get('dbname', '?')}'"
    return (f"Can't reach Postgres at {where}. Check that Docker Desktop is running, then start the database with "
            "`docker compose up -d db` and wait until `docker compose ps` shows it as healthy. "
            "DATABASE_URL in .env must match docker-compose.yml (default: postgresql://app:app@127.0.0.1:5433/veltrix).")


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    url = url or settings.database_url
    try:
        conn = psycopg.connect(url, connect_timeout=5)
    except psycopg.OperationalError as e:
        raise psycopg.OperationalError(f"{e}\n{unreachable_hint(url)}") from None
    with conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        yield conn


def init_schema(conn: psycopg.Connection, reset: bool = False) -> None:
    dim = int(settings.embed_dim)  # int() guards the only formatted value in DDL
    if reset:
        conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id          BIGSERIAL PRIMARY KEY,
            doc_name    TEXT NOT NULL,
            chunk_index INT  NOT NULL,
            content     TEXT NOT NULL,
            metadata    JSONB DEFAULT '{{}}',
            embedding   vector({dim}) NOT NULL,
            UNIQUE (doc_name, chunk_index)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)")
    existing = conn.execute(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    if existing and existing[0] != dim:
        raise RuntimeError(
            f"chunks.embedding has {existing[0]} dims but EMBED_DIM={dim}. "
            "Run: uv run -m veltrix_copilot.knowledge.ingest --reset"
        )


def replace_document(conn: psycopg.Connection, doc_name: str, chunks: list[str], vectors: list[list[float]],
                     metadata: dict | list[dict] | None = None) -> None:
    """Idempotent: deletes the document's old chunks, then inserts the new ones.
    `metadata` is one dict for every chunk, or one dict per chunk (e.g. {"page": 3})."""
    import json

    metas = metadata if isinstance(metadata, list) else [metadata or {}] * len(chunks)
    with conn.transaction():
        conn.execute("DELETE FROM chunks WHERE doc_name = %s", (doc_name,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (doc_name, chunk_index, content, metadata, embedding) VALUES (%s, %s, %s, %s, %s)",
                [(doc_name, i, c, json.dumps(m), Vector(v)) for i, (c, v, m) in enumerate(zip(chunks, vectors, metas))],
            )


def delete_document(conn: psycopg.Connection, doc_name: str) -> int:
    return conn.execute("DELETE FROM chunks WHERE doc_name = %s", (doc_name,)).rowcount


def _chunk(r, distance: float = 0.0) -> dict:
    meta = r[3] or {}
    return {"doc_name": r[0], "chunk_index": r[1], "content": r[2], "distance": distance,
            "page": meta.get("page"), "section": meta.get("section")}


def search(conn: psycopg.Connection, query_vec: list[float], k: int, exclude: list[str] | None = None) -> list[dict]:
    """The k nearest chunks, leaving out the documents in `exclude` (older versions)."""
    rows = conn.execute(
        """SELECT doc_name, chunk_index, content, metadata, embedding <=> %s AS distance
           FROM chunks WHERE NOT (doc_name = ANY(%s)) ORDER BY embedding <=> %s LIMIT %s""",
        (Vector(query_vec), list(exclude or []), Vector(query_vec), k),
    ).fetchall()
    return [_chunk(r, float(r[4])) for r in rows]


def doc_names(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT doc_name FROM chunks ORDER BY doc_name").fetchall()]


def document_chunks(conn: psycopg.Connection, doc_name: str, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_name, chunk_index, content, metadata FROM chunks WHERE doc_name = %s ORDER BY chunk_index LIMIT %s",
        (doc_name, limit),
    ).fetchall()
    return [_chunk(r) for r in rows]


def stats(conn: psycopg.Connection) -> dict:
    rows = conn.execute("SELECT doc_name, count(*) FROM chunks GROUP BY doc_name ORDER BY doc_name").fetchall()
    return {r[0]: r[1] for r in rows}
