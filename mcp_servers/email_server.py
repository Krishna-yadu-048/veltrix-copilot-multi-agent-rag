"""Email MCP server. DRAFTS ONLY: there is intentionally no send tool. Drafts are files in data/outbox."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_servers.common import atomic_write_json, data_dir, env, make_server, read_json, safe_child

mcp, PORT = make_server("email", 8102)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$")


def _outbox() -> Path:
    d = data_dir() / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _allowed_domains() -> set[str]:
    return {d.strip().lower() for d in env("ALLOWED_EMAIL_DOMAINS", "veltrix.io").split(",") if d.strip()}


@mcp.tool()
def draft_email(to: str, subject: str, body: str) -> dict:
    """Save an email DRAFT for the user to review. Nothing is sent.

    Args:
        to: recipient address (must be in an allowed company domain)
        subject: subject line (max 200 characters)
        body: plain-text body (max 10000 characters)
    """
    m = EMAIL_RE.match(to.strip())
    if not m:
        raise ValueError("Invalid recipient address")
    if m.group(1).lower() not in _allowed_domains():
        raise ValueError("Recipient domain is not allowed for drafts")
    if not subject.strip() or len(subject) > 200:
        raise ValueError("Subject must be 1-200 characters")
    if len(body) > 10_000:
        raise ValueError("Body is too long")

    draft_id = uuid.uuid4().hex  # server-generated name: never derived from model text
    path = safe_child(_outbox(), f"{draft_id}.json")
    draft = {
        "draft_id": draft_id,
        "to": to.strip(),
        "subject": subject.strip(),
        "body": body,
        "status": "draft",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_json(path, draft)
    return {"draft_id": draft_id, "status": "draft", "to": draft["to"], "subject": draft["subject"]}


@mcp.tool()
def list_drafts(limit: int = 20, include_body: bool = False) -> list[dict]:
    """List saved drafts, newest first. Set include_body=true to also return each body."""
    drafts = [read_json(p, None) for p in _outbox().glob("*.json")]
    drafts = [d for d in drafts if d]
    drafts.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    keys = ("draft_id", "to", "subject", "created_at", "status") + (("body",) if include_body else ())
    return [{k: d.get(k) for k in keys} for d in drafts[: max(1, min(limit, 100))]]


@mcp.tool()
def get_draft(draft_id: str) -> dict:
    """Return one draft, including its body."""
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
        raise ValueError("Invalid draft id")
    draft = read_json(safe_child(_outbox(), f"{draft_id}.json"), None)
    if not draft:
        raise ValueError("Draft not found")
    return draft


@mcp.tool()
def delete_draft(draft_id: str) -> dict:
    """Delete one saved draft (drafts are never sent, so this only removes it from the outbox)."""
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
        raise ValueError("Invalid draft id")
    path = safe_child(_outbox(), f"{draft_id}.json")
    if not path.exists():
        return {"draft_id": draft_id, "deleted": False}
    path.unlink()
    return {"draft_id": draft_id, "deleted": True}


if __name__ == "__main__":
    print(f"email MCP server on http://127.0.0.1:{PORT}/mcp")
    mcp.run(transport="streamable-http")
