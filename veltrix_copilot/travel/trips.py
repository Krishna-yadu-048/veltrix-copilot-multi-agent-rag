"""Trip register: every planned trip gets its own row and a short number (Trip #7)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from veltrix_copilot.knowledge import db

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id              SERIAL PRIMARY KEY,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    plan            JSONB NOT NULL,
    session_id      TEXT,
    draft_ids       JSONB NOT NULL DEFAULT '[]',
    calendar_events INT  NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trips_destination_idx ON trips (lower(destination), start_date);
ALTER TABLE trips ADD COLUMN IF NOT EXISTS calendar_event_ids JSONB NOT NULL DEFAULT '[]';
ALTER TABLE trips ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS replaced_by INT;
CREATE TABLE IF NOT EXISTS trip_versions (
    id          SERIAL PRIMARY KEY,
    trip_id     INT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    plan        JSONB NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trip_versions_trip_idx ON trip_versions (trip_id, id);
"""

_COLS = ("id, origin, destination, start_date, end_date, plan, session_id, draft_ids, calendar_events, created_at, "
         "calendar_event_ids, cancelled_at, updated_at, replaced_by, "
         "(SELECT count(*) FROM trip_versions v WHERE v.trip_id = trips.id)")


class TripStoreUnavailable(Exception):
    """The database can't be reached; callers fall back to chat memory."""


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def _row(r) -> dict:
    plan = dict(r[5])
    plan["trip_id"] = r[0]
    if r[11]:
        plan["cancelled"] = True
    return {"id": r[0], "origin": r[1], "destination": r[2], "start": r[3].isoformat(), "end": r[4].isoformat(),
            "plan": plan, "session_id": r[6], "draft_ids": list(r[7] or []), "calendar_events": r[8],
            "created_at": r[9].isoformat(timespec="seconds"), "calendar_event_ids": list(r[10] or []),
            "cancelled_at": r[11].isoformat(timespec="seconds") if r[11] else None,
            "updated_at": r[12].isoformat(), "replaced_by": r[13], "versions": int(r[14] or 0)}


def add(conn: psycopg.Connection, plan: dict, session_id: str | None) -> int:
    row = conn.execute(
        """INSERT INTO trips (origin, destination, start_date, end_date, plan, session_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (plan["origin"], plan["destination"], date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"]),
         Jsonb({k: v for k, v in plan.items() if k != "trip_id"}), session_id)).fetchone()
    return int(row[0])


def get(conn: psycopg.Connection, trip_id: int) -> dict | None:
    r = conn.execute(f"SELECT {_COLS} FROM trips WHERE id = %s", (trip_id,)).fetchone()
    return _row(r) if r else None


def list_trips(conn: psycopg.Connection, limit: int = 200) -> list[dict]:
    """Newest first."""
    rows = conn.execute(f"SELECT {_COLS} FROM trips ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [_row(r) for r in rows]


def mark_email(conn: psycopg.Connection, trip_id: int, draft_id: str) -> None:
    conn.execute("""UPDATE trips SET draft_ids = draft_ids || %s, updated_at = now() WHERE id = %s""",
                 (Jsonb([draft_id]), trip_id))


def forget_draft(conn: psycopg.Connection, draft_id: str) -> None:
    """A draft was deleted: drop it from any trip that pointed to it (so 'emailed' stays accurate)."""
    conn.execute("""UPDATE trips SET draft_ids = draft_ids - %s, updated_at = now()
                    WHERE draft_ids ? %s""", (draft_id, draft_id))


def mark_calendar(conn: psycopg.Connection, trip_id: int, events: int, event_ids: list[str] | None = None) -> None:
    """Record the trip's calendar events (their ids are needed to remove them if the trip changes or is cancelled)."""
    conn.execute("""UPDATE trips SET calendar_events = %s, updated_at = now(),
                        calendar_event_ids = (SELECT COALESCE(jsonb_agg(DISTINCT e), '[]')
                                              FROM jsonb_array_elements(calendar_event_ids || %s) AS e)
                    WHERE id = %s""",
                 (events, Jsonb([i for i in (event_ids or []) if i]), trip_id))


def clear_calendar(conn: psycopg.Connection, trip_id: int) -> None:
    conn.execute("UPDATE trips SET calendar_events = 0, calendar_event_ids = '[]', updated_at = now() WHERE id = %s",
                 (trip_id,))


def update_plan(conn: psycopg.Connection, trip_id: int, plan: dict) -> None:
    """Save an edited plan under the same trip number (new flights, nights or hotel)."""
    conn.execute("""UPDATE trips SET plan = %s, start_date = %s, end_date = %s, updated_at = now() WHERE id = %s""",
                 (Jsonb({k: v for k, v in plan.items() if k not in ("trip_id", "cancelled")}),
                  date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"]), trip_id))


def cancel(conn: psycopg.Connection, trip_id: int, replaced_by: int | None = None) -> None:
    conn.execute("""UPDATE trips SET cancelled_at = now(), replaced_by = %s, updated_at = now()
                    WHERE id = %s AND cancelled_at IS NULL""", (replaced_by, trip_id))


def uncancel(conn: psycopg.Connection, trip_id: int) -> None:
    conn.execute("UPDATE trips SET cancelled_at = NULL, replaced_by = NULL, updated_at = now() WHERE id = %s",
                 (trip_id,))


def save_version(conn: psycopg.Connection, trip_id: int, plan: dict, reason: str) -> None:
    conn.execute("INSERT INTO trip_versions (trip_id, plan, reason) VALUES (%s, %s, %s)",
                 (trip_id, Jsonb({k: v for k, v in plan.items() if k not in ("trip_id", "cancelled")}), reason[:200]))


def list_versions(conn: psycopg.Connection, trip_id: int) -> list[dict]:
    """Previous versions, newest first."""
    rows = conn.execute("SELECT id, plan, reason, created_at FROM trip_versions WHERE trip_id = %s ORDER BY id DESC",
                        (trip_id,)).fetchall()
    return [{"id": r[0], "plan": dict(r[1]), "reason": r[2], "created_at": r[3].isoformat(timespec="seconds")}
            for r in rows]


def pop_version(conn: psycopg.Connection, trip_id: int) -> dict | None:
    """Remove and return the most recent previous version (used by undo)."""
    row = conn.execute("""DELETE FROM trip_versions WHERE id = (SELECT max(id) FROM trip_versions WHERE trip_id = %s)
                          RETURNING plan, reason""", (trip_id,)).fetchone()
    return {"plan": dict(row[0]), "reason": row[1]} if row else None


def status(trip: dict) -> str:
    """'planned', 'emailed', 'in calendar', 'emailed, in calendar' or 'cancelled'."""
    if trip.get("cancelled_at"):
        return "cancelled"
    bits = (["emailed"] if trip.get("draft_ids") else []) + (["in calendar"] if trip.get("calendar_events") else [])
    return ", ".join(bits) or "planned"


_READY: set[str] = set()   # databases where the table has been created in this process


def _run(fn, *args):
    from veltrix_copilot.core.config import settings
    with db.connect() as conn:
        if settings.database_url not in _READY:
            init_schema(conn)
            _READY.add(settings.database_url)
        return fn(conn, *args)


async def call(fn, *args):
    """Run one store function in its own short connection, off the event loop."""
    try:
        return await asyncio.to_thread(_run, fn, *args)
    except Exception as e:
        log.warning("trip store unavailable: %s", str(e).splitlines()[0][:200])
        raise TripStoreUnavailable(str(e)) from e


async def try_call(fn, *args, default=None):
    """Like call(), but returns `default` when the database is down (used for best-effort updates)."""
    try:
        return await call(fn, *args)
    except TripStoreUnavailable:
        return default
