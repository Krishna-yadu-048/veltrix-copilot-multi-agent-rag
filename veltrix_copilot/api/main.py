"""FastAPI layer, the UI's only entry point:  uvicorn veltrix_copilot.api.main:app --port 8000
Chats (/session, /sessions, /chat), /health, /system, /admin/reset, documents (/documents), saved trips
(/trips, preview/apply/undo/history, /cities) and the tools for the sidebar (/drafts, /events, info)."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import PurePath, PurePosixPath

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from veltrix_copilot.api import chat_store
from veltrix_copilot.knowledge import db, doc_versions
from veltrix_copilot.core import llm
from veltrix_copilot.travel import trip_service, trips
from veltrix_copilot.agents import travel_agent
from veltrix_copilot.core.config import settings
from veltrix_copilot.knowledge import ingest as ingest_mod
from veltrix_copilot.knowledge.loaders import SUPPORTED
from veltrix_copilot.tools.mcp_client import ToolError, ToolUnavailable, call_tool, list_tools
from veltrix_copilot.orchestrator.graph import build_graph, run_turn
from veltrix_copilot.core.schemas import ChatRequest, ChatResponse, ResetRequest, Source, TripApply, TripChange

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

STATE: dict = {}
SESSION_ID = Path(..., pattern=r"^[0-9a-f]{32}$")
TRIP_ID = Path(..., ge=1)
DB_DOWN = "Database unavailable. Is Postgres running? (docker compose up -d db)"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_SIGNATURES = {".pdf": b"%PDF", ".docx": b"PK"}


def _store(fn, *args):
    with db.connect() as conn:
        return fn(conn, *args)


async def store(fn, *args):
    """A chat_store function in its own short DB connection, off the event loop."""
    try:
        return await asyncio.to_thread(_store, fn, *args)
    except Exception as e:
        log.error("chat store error: %s", str(e).splitlines()[0])
        raise HTTPException(503, DB_DOWN)


async def _init_schema():
    await store(chat_store.init_schema)
    await store(trips.init_schema)
    STATE["schema_ready"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["graph"] = build_graph()
    try:
        await _init_schema()
    except HTTPException:
        log.warning("Could not create the chat tables yet (database down?)")
    log.info("Graph ready. Chat model=%s, embed model=%s", settings.chat_model, settings.embed_model)
    yield


app = FastAPI(title="Veltrix Copilot", version="1.3.0", lifespan=lifespan)


def _error(status: int, message: str):
    async def handler(request, exc):
        return JSONResponse({"detail": getattr(exc, "message", message)}, status_code=getattr(exc, "status", status))
    return handler


app.add_exception_handler(trip_service.TripError, _error(422, ""))
app.add_exception_handler(trips.TripStoreUnavailable, _error(503, DB_DOWN))
app.add_exception_handler(ToolUnavailable, _error(503, "A tool server isn't running. Start everything with `uv run scripts/dev.py`."))


@app.post("/session")
async def new_session() -> dict:
    if not STATE.get("schema_ready"):        # the DB may have been down when the API started
        await _init_schema()
    return {"session_id": await store(chat_store.create_session)}


@app.get("/sessions")
async def sessions(limit: int = 50) -> list[dict]:
    return await store(chat_store.list_sessions, max(1, min(limit, 200)))


@app.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str = SESSION_ID) -> list[dict]:
    if not await store(chat_store.session_exists, session_id):
        raise HTTPException(404, "Unknown chat")
    return await store(chat_store.get_messages, session_id)


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str = SESSION_ID) -> dict:
    if not await store(chat_store.delete_session, session_id):
        raise HTTPException(404, "Unknown chat")
    return {"deleted": session_id}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    t0 = time.perf_counter()
    if not await store(chat_store.session_exists, req.session_id):
        raise HTTPException(404, "Unknown chat. Start a new one with POST /session.")
    memory = await store(chat_store.get_memory, req.session_id)        # from the DB: survives restarts
    message = req.message.strip()
    try:
        state = await asyncio.wait_for(run_turn(STATE["graph"], req.session_id, message, memory=memory),
                                       timeout=settings.request_timeout_seconds)
    except asyncio.TimeoutError:
        raise HTTPException(504, "The assistant took too long. The model may have spilled out of GPU memory; check `ollama ps`.")
    except Exception:
        log.exception("chat failed")
        raise HTTPException(500, "Internal error. Check the API logs.")
    timings = {**(state.get("timings") or {}), "total": round(time.perf_counter() - t0, 3)}
    agents_run = list(state.get("results") or {})
    response = ChatResponse(answer=state.get("final_answer", ""), sources=[Source(**s) for s in state.get("sources") or []],
                            actions=state.get("actions") or [], plan=agents_run, timings=timings,
                            ask="yes_no" if ((state.get("memory") or {}).get("pending") or {}).get("kind") == "confirm" else None)
    await store(chat_store.save_turn, req.session_id, message, response.answer,
                response.model_dump(exclude={"answer"}), state.get("memory") or {})
    log.info("session=%s… agents=%s timings=%s", req.session_id[:6], agents_run, timings)   # never message text
    return response


@app.get("/health")
async def health() -> dict:
    out: dict = {}
    try:
        counts = _store(db.stats)
        out["database"] = {"ok": True, "documents": len(counts), "chunks": sum(counts.values())}
    except Exception as e:
        msg = str(e).splitlines()[0][:200]
        if "chunks" in msg and "does not exist" in msg:
            msg = "No documents ingested yet. Run: uv run -m veltrix_copilot.knowledge.ingest"
        out["database"] = {"ok": False, "error": msg}
    out["ollama"] = await asyncio.to_thread(llm.status)
    for server in ("travel", "email", "calendar"):
        try:
            out[f"mcp_{server}"] = {"ok": True, "tools": await list_tools(server)}
            if server != "email":                    # backend status and any warning
                info = await call_tool(server, f"{server}_info", {})
                out[f"mcp_{server}"].update({"backend": info.get("backend")}, **({"warning": info["error"]} if info.get("error") else {}))
        except (ToolUnavailable, ToolError):
            out.setdefault(f"mcp_{server}", {"ok": False})
    out["ok"] = all(v.get("ok") for v in out.values() if isinstance(v, dict))
    return out


@app.get("/system")
async def system() -> dict:
    """Everything the Status page shows. No passwords, tokens or API keys."""
    s = settings
    return {
        "api": {"url": s.api_url, "version": app.version, "python": platform.python_version(),
                "request_timeout_s": s.request_timeout_seconds},
        "model": {"chat_model": s.chat_model, "embed_model": s.embed_model, "embed_dim": s.embed_dim,
                  "context_tokens": s.num_ctx, "embeddings_on_cpu": s.embed_on_cpu,
                  "thinking_disabled": s.disable_thinking, "ollama_url": s.ollama_url, "keep_alive": s.keep_alive},
        "rag": {"top_k": s.rag_top_k, "relevance_gate": s.rag_max_distance, "chunk_size": s.chunk_size,
                "chunk_overlap": s.chunk_overlap, "max_context_chars": s.rag_max_context_chars},
        "agents": {"llm_followups": s.use_llm_followups},
        "user": {"home_city": s.home_city, "manager_email": s.manager_email, "name": s.user_name},
        "services": {"database": re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", s.database_url),
                     "travel": s.mcp_travel_url, "email": s.mcp_email_url, "calendar": s.mcp_calendar_url},
    }


async def _delete_all(server: str, list_tool: str, list_args: dict, delete_tool: str, id_key: str) -> int:
    """Delete everything a (capped) list tool returns, in rounds, until nothing is left."""
    removed = 0
    for _ in range(50):
        before = removed
        for item in await call_tool(server, list_tool, list_args):
            removed += bool((await call_tool(server, delete_tool, {id_key: item[id_key]})).get("deleted"))
        if removed == before:
            break
    return removed


def _wipe_database(conn) -> dict:
    chat_store.init_schema(conn)
    trips.init_schema(conn)
    n = {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in ("trips", "chat_sessions")}
    conn.execute("TRUNCATE trips, trip_versions RESTART IDENTITY")
    conn.execute("DELETE FROM chat_sessions")          # messages go with them (ON DELETE CASCADE)
    return {"trips": n["trips"], "chats": n["chat_sessions"]}


@app.post("/admin/reset")
async def full_reset(req: ResetRequest) -> dict:
    """Delete every chat, email draft, trip and assistant calendar event (documents are kept). Needs {"confirm": "CONFIRM"}."""
    if req.confirm != "CONFIRM":
        raise HTTPException(400, "Type CONFIRM (in capitals) to reset everything.")
    out: dict = {"errors": []}
    for key, args in (("calendar_events", ("calendar", "list_events", {"from_date": "1970-01-01"}, "delete_event", "event_id")),
                      ("email_drafts", ("email", "list_drafts", {"limit": 100}, "delete_draft", "draft_id"))):
        try:
            out[key] = await _delete_all(*args)
        except (ToolUnavailable, ToolError) as e:
            out["errors"].append(f"{args[0]}: {e}")
    try:
        out.update(await asyncio.to_thread(_store, _wipe_database))
    except Exception as e:
        out["errors"].append("database: " + str(e).splitlines()[0][:200])
    STATE["graph"] = build_graph()                         # forget in-memory chat state too
    log.warning("FULL RESET: %s", {k: v for k, v in out.items() if k != "errors"} | {"errors": len(out["errors"])})
    return out


UPLOAD_DIR = "uploads"


def safe_doc_name(name: str) -> str:
    """A plain file name: no folders, no odd characters, a supported extension."""
    stem, ext = os.path.splitext(re.sub(r"[^A-Za-z0-9._ -]", "_", PurePath(name or "").name).strip(" ."))
    if ext.lower() not in SUPPORTED:
        raise HTTPException(415, f"Unsupported file type. Upload one of: {', '.join(sorted(SUPPORTED))}")
    if not stem or len(stem + ext) > 100:
        raise HTTPException(400, "File name must be 1-100 characters")
    return stem + ext.lower()


def safe_doc_path(name: str) -> str:
    """A document's path inside data/docs (e.g. 'policies/pol-hr-001-leave-policy.md'): no '..', no hidden parts."""
    parts = PurePosixPath((name or "").replace("\\", "/")).parts
    if not parts or len(parts) > 6 or any(p in ("", ".", "..") or p.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._ -]+", p)
                                          for p in parts):
        raise HTTPException(400, "Invalid document path")
    return "/".join(parts[:-1] + (safe_doc_name(parts[-1]),))


def _files() -> dict:
    return {ingest_mod.doc_name(p): p.stat() for p in ingest_mod.document_files()}


def _superseded(names: list[str]) -> dict:
    return doc_versions.superseded(names, doc_versions.file_times(settings.docs_dir, names))


def _document_rows() -> list[dict]:
    counts = _store(db.stats)
    files = _files()
    names = sorted(set(files) | set(counts))
    replaced = _superseded(names)
    return [{"name": n, "folder": n.rsplit("/", 1)[0] if "/" in n else "", "chunks": counts.get(n, 0), "indexed": n in counts,
             "size_kb": round(files[n].st_size / 1024, 1) if n in files else None,
             "added": datetime.fromtimestamp(files[n].st_mtime).isoformat(timespec="seconds") if n in files else None,
             "superseded_by": replaced.get(n)} for n in names]


@app.get("/documents")
async def documents() -> list[dict]:
    try:
        return await asyncio.to_thread(_document_rows)
    except Exception as e:
        log.error("documents error: %s", str(e).splitlines()[0])
        raise HTTPException(503, DB_DOWN)


def _store_and_index(name: str, data: bytes) -> dict:
    """Index a hidden temporary copy, then move it into place: a failure leaves the old version untouched."""
    tmp = settings.docs_dir / f".upload-{uuid.uuid4().hex}{os.path.splitext(name)[1]}"
    tmp.write_bytes(data)
    try:
        with db.connect() as conn:
            db.init_schema(conn)
            n = ingest_mod.ingest_file(conn, tmp, name=name)
        if not n:
            raise HTTPException(422, "No text found in that file (a scanned PDF needs OCR first).")
        (settings.docs_dir / name).parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, settings.docs_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    replaced = _superseded(list(_files()))
    return {"name": name, "chunks": n, "replaces": sorted(old for old, new in replaced.items() if new == name),
            "superseded_by": replaced.get(name)}


@app.post("/documents")
async def upload_document(request: Request, name: str = Query(..., min_length=1, max_length=200)) -> dict:
    """Add or replace a document (the body is the raw file): checked, saved to data/docs/uploads and indexed."""
    name, data = f"{UPLOAD_DIR}/{safe_doc_name(name)}", await request.body()
    ext = os.path.splitext(name)[1]
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 20 MB)")
    if ext in _SIGNATURES and not data.startswith(_SIGNATURES[ext]):
        raise HTTPException(415, f"That doesn't look like a real {ext} file")
    if ext in (".md", ".txt"):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "Text files must be UTF-8")
    try:
        result = await asyncio.to_thread(_store_and_index, name, data)
    except HTTPException:
        raise
    except llm.LLMUnavailable:
        raise HTTPException(503, "The embedding model isn't responding. Is Ollama running?")
    except Exception as e:
        log.error("upload failed: %s", str(e).splitlines()[0][:200])
        raise HTTPException(422, "Could not read or index that file.")
    log.info("document added: %s (%s chunks)", name, result["chunks"])
    return result


def _remove_document(name: str) -> bool:
    path = settings.docs_dir / name
    removed = _store(db.delete_document, name) > 0 or path.exists()
    path.unlink(missing_ok=True)
    return removed


@app.delete("/documents/{name:path}")
async def delete_document(name: str) -> dict:
    name = safe_doc_path(name)
    try:
        removed = await asyncio.to_thread(_remove_document, name)
    except Exception as e:
        log.error("remove failed: %s", str(e).splitlines()[0][:200])
        raise HTTPException(503, "Database unavailable.")
    if not removed:
        raise HTTPException(404, "No such document")
    return {"deleted": name}


@app.get("/trips")
async def trip_list(limit: int = 50) -> list[dict]:
    return [{"id": r["id"], "origin": r["origin"], "destination": r["destination"], "start": r["start"],
             "end": r["end"], "total_usd": (r["plan"].get("costs") or {}).get("total"), "status": trips.status(r),
             "emails": len(r["draft_ids"]), "calendar_events": r["calendar_events"], "created_at": r["created_at"]}
            for r in await trips.call(trips.list_trips, max(1, min(limit, 200)))]


async def _trip(trip_id: int) -> dict:
    if not (row := await trips.call(trips.get, trip_id)):
        raise HTTPException(404, "No such trip")
    return row


@app.get("/cities")
async def city_list() -> list[str]:
    return trip_service.cities()


@app.get("/trips/{trip_id}")
async def trip_detail(trip_id: int = TRIP_ID) -> dict:
    r = await _trip(trip_id)
    return {"id": r["id"], "status": trips.status(r), "plan": r["plan"], "updated_at": r["updated_at"],
            "versions": r["versions"], "replaced_by": r["replaced_by"], "emails": len(r["draft_ids"]),
            "calendar_events": len(r["calendar_event_ids"]) or r["calendar_events"],
            "created_at": r["created_at"], "markdown": travel_agent.render_plan(r["plan"])}


@app.get("/trips/{trip_id}/history")
async def trip_history(trip_id: int = TRIP_ID) -> list[dict]:
    return await trip_service.history(trip_id)


@app.post("/trips/{trip_id}/preview")
async def trip_preview(change: TripChange, trip_id: int = TRIP_ID) -> dict:
    return await trip_service.preview(trip_id, change.as_change())


@app.post("/trips/{trip_id}/apply")
async def trip_apply(req: TripApply, trip_id: int = TRIP_ID) -> dict:
    result = await trip_service.apply(trip_id, req.as_change(), req.base_updated_at, email=req.email, calendar=req.calendar)
    log.info("trip %s edited -> trip %s (%s)", trip_id, result["trip_id"], "new" if result["new_trip"] else "same")
    return result


@app.post("/trips/{trip_id}/undo")
async def trip_undo(trip_id: int = TRIP_ID) -> dict:
    return await trip_service.undo(trip_id)


@app.get("/drafts")
async def drafts(include_body: bool = False) -> list[dict]:
    return await call_tool("email", "list_drafts", {"limit": 20, "include_body": include_body})


@app.get("/drafts/{draft_id}")
async def draft(draft_id: str) -> dict:
    try:
        return await call_tool("email", "get_draft", {"draft_id": draft_id})
    except ToolError:
        raise HTTPException(404, "draft not found")


@app.delete("/drafts/{draft_id}")
async def delete_draft(draft_id: str = Path(..., pattern=r"^[0-9a-f]{32}$")) -> dict:
    """Delete a draft; trips that referred to it no longer count it as emailed."""
    if not (res := await call_tool("email", "delete_draft", {"draft_id": draft_id})).get("deleted"):
        raise HTTPException(404, "draft not found")
    await trips.try_call(trips.forget_draft, draft_id)
    return res


@app.get("/events")
async def events() -> list[dict]:
    try:
        return await call_tool("calendar", "list_events", {})
    except ToolError as e:          # e.g. Google Calendar not connected / sign-in expired
        raise HTTPException(502, str(e))


@app.get("/calendar/info")
async def calendar_info() -> dict:
    return await call_tool("calendar", "calendar_info", {})


@app.get("/travel/info")
async def travel_info() -> dict:
    return await call_tool("travel", "travel_info", {})
