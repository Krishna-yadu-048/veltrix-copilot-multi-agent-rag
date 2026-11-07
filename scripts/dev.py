"""Start everything except Postgres and Ollama, in one terminal. Ctrl+C stops all of it."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

SERVICES = [
    ("mcp-travel", [PY, "mcp_servers/travel_server.py", "--port", "8101"]),
    ("mcp-email", [PY, "mcp_servers/email_server.py", "--port", "8102"]),
    ("mcp-calendar", [PY, "mcp_servers/calendar_server.py", "--port", "8103"]),
    ("api", [PY, "-m", "uvicorn", "veltrix_copilot.api.main:app", "--host", "127.0.0.1", "--port", "8000"]),
    ("ui", [PY, "-m", "streamlit", "run", "ui/app.py",
            "--server.address", "127.0.0.1", "--server.port", "8501", "--browser.gatherUsageStats", "false"]),
]
PORTS = [8101, 8102, 8103, 8000, 8501]


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    busy = [p for p in PORTS if port_in_use(p)]
    if busy:
        sys.exit(f"Port(s) {busy} already in use. Is another copy of dev.py (or an old server) still running? Stop it first.")

    # Treat SIGTERM like Ctrl+C so child processes are always cleaned up.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, cmd in SERVICES:
            print(f"starting {name:<13} {' '.join(cmd[1:])}")
            procs.append((name, subprocess.Popen(cmd, cwd=ROOT, env=env)))
            time.sleep(1.5 if name.startswith("mcp") else 0.5)
        print("\nUI:  http://127.0.0.1:8501\nAPI: http://127.0.0.1:8000/docs\nPress Ctrl+C to stop.\n")
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"\n{name} exited with code {p.returncode}; stopping everything.")
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # a second stop signal must not interrupt the cleanup
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()  # SIGTERM on Linux/macOS, TerminateProcess on Windows
        for _, p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        print("stopped.")


if __name__ == "__main__":
    main()
