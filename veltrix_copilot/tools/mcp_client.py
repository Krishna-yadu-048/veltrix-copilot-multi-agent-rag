"""MCP client for the agents: one short streamable-HTTP session per call (a restarted server just works)."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from veltrix_copilot.core.config import settings


class ToolError(RuntimeError):
    """The tool ran but refused (bad arguments, validation failure...)."""


class ToolUnavailable(RuntimeError):
    """The MCP server could not be reached."""


def server_url(server: str) -> str:
    return {"travel": settings.mcp_travel_url, "email": settings.mcp_email_url,
            "calendar": settings.mcp_calendar_url}[server]


async def _session(server: str, work):
    url = server_url(server)
    try:
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=settings.mcp_timeout_seconds)) as s:
                await s.initialize()
                return await work(s)
    except Exception as e:      # connection refused, timeouts, protocol errors
        raise ToolUnavailable(f"MCP server '{server}' at {url} is unreachable: {e}") from e


async def call_tool(server: str, tool: str, args: dict | None = None) -> Any:
    result = await _session(server, lambda s: s.call_tool(tool, args or {}))
    if result.isError:
        msg = " ".join(getattr(c, "text", "") for c in result.content).strip()
        raise ToolError(re.sub(r"^Error executing tool \w+: ", "", msg) or "tool error")
    sc = result.structuredContent
    if sc is None:                                   # tools without an output schema answer in a text block
        text = "".join(getattr(c, "text", "") for c in result.content)
        return json.loads(text) if text[:1] in "[{" and text else text
    return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc   # FastMCP wraps lists


async def list_tools(server: str) -> list[str]:
    return [t.name for t in (await _session(server, lambda s: s.list_tools())).tools]
