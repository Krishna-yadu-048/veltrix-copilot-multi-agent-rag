"""Persistent chat history in Postgres."""
from __future__ import annotations

import uuid

import psycopg
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New chat',
    memory      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    meta        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages (session_id, id);
"""


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def create_session(conn: psycopg.Connection) -> str:
    sid = uuid.uuid4().hex
    conn.execute("INSERT INTO chat_sessions (id) VALUES (%s)", (sid,))
    return sid


def session_exists(conn: psycopg.Connection, sid: str) -> bool:
    return conn.execute("SELECT 1 FROM chat_sessions WHERE id = %s", (sid,)).fetchone() is not None


def get_memory(conn: psycopg.Connection, sid: str) -> dict:
    row = conn.execute("SELECT memory FROM chat_sessions WHERE id = %s", (sid,)).fetchone()
    return dict(row[0]) if row and row[0] else {}


def make_title(message: str, limit: int = 60) -> str:
    one_line = " ".join(message.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def save_turn(conn: psycopg.Connection, sid: str, user_text: str, answer: str, meta: dict, memory: dict) -> None:
    """Store one user message + assistant answer and the updated session memory, atomically."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'user', %s)", (sid, user_text))
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, meta) VALUES (%s, 'assistant', %s, %s)",
            (sid, answer, Jsonb(meta)))
        # The first message becomes the chat title.
        conn.execute(
            """UPDATE chat_sessions
               SET memory = %s, updated_at = now(),
                   title = CASE WHEN title = 'New chat' THEN %s ELSE title END
               WHERE id = %s""",
            (Jsonb(memory), make_title(user_text), sid))


def list_sessions(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    """Chats that have at least one message, most recently used first."""
    rows = conn.execute(
        """SELECT s.id, s.title, s.updated_at, count(m.id)
           FROM chat_sessions s JOIN chat_messages m ON m.session_id = s.id
           GROUP BY s.id ORDER BY s.updated_at DESC LIMIT %s""",
        (limit,)).fetchall()
    return [{"session_id": r[0], "title": r[1], "updated_at": r[2].isoformat(timespec="seconds"),
             "messages": r[3]} for r in rows]


def get_messages(conn: psycopg.Connection, sid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, meta, created_at FROM chat_messages WHERE session_id = %s ORDER BY id",
        (sid,)).fetchall()
    out = []
    for role, content, meta, created in rows:
        item = {"role": role, "created_at": created.isoformat(timespec="seconds")}
        if role == "user":
            item["text"] = content
        else:
            item.update({"answer": content, **(meta or {})})
        out.append(item)
    return out


def delete_session(conn: psycopg.Connection, sid: str) -> bool:
    return conn.execute("DELETE FROM chat_sessions WHERE id = %s", (sid,)).rowcount > 0
