"""Shared helpers for the MCP servers: settings, local-only server set-up, safe JSON files."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

ROOT = Path(__file__).resolve().parents[1]


def _dotenv() -> dict[str, str]:
    """Minimal .env reader (the servers don't import the app package or pydantic-settings)."""
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in "\"'" and value[:1] and value.count(value[0]) >= 2:   # "quoted" or 'quoted'
                value = value[1:value.index(value[0], 1)]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()     # KEY=value   # a comment
            values[key.strip()] = value
    return values


def env(key: str, default: str = "") -> str:
    """Real environment variables win over .env, which wins over the default."""
    if key in os.environ:
        return os.environ[key]
    return _dotenv().get(key, default)


def env_path(key: str, default: Path) -> Path:
    """A file path setting; a relative path is relative to the project folder, wherever the server starts."""
    path = Path(env(key, "") or default).expanduser()
    return path if path.is_absolute() else ROOT / path


def data_dir() -> Path:
    d = Path(env("MCP_DATA_DIR", str(ROOT / "data"))).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_server(name: str, default_port: int) -> tuple[FastMCP, int]:
    parser = argparse.ArgumentParser(description=f"{name} MCP server")
    parser.add_argument("--port", type=int, default=default_port)
    args, _ = parser.parse_known_args()
    # Localhost only + DNS-rebinding protection: a malicious web page cannot call these tools.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        allowed_origins=["http://127.0.0.1", "http://127.0.0.1:*", "http://localhost", "http://localhost:*"],
    )
    # Stateless + plain JSON replies: the client opens one short connection per tool call, so no session or
    # background event stream is kept (that stream logged "ClosedResourceError" each time a call finished).
    server = FastMCP(name, host="127.0.0.1", port=args.port, log_level="WARNING", transport_security=security,
                     stateless_http=True, json_response=True)
    return server, args.port


def safe_child(base: Path, filename: str) -> Path:
    """Resolve a file inside `base`, refusing anything that escapes it (path traversal)."""
    path = (base / filename).resolve()
    if base.resolve() not in path.parents:
        raise ValueError("Invalid file name")
    return path


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
