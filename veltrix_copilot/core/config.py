"""All settings, from environment variables or .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # Local LLM
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b-instruct-2507-q4_K_M"  # non-thinking Qwen3 build
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    num_ctx: int = 4096
    disable_thinking: bool = True
    embed_on_cpu: bool = True
    llm_timeout_seconds: float = 240.0
    keep_alive: str = "30m"

    # Database
    database_url: str = "postgresql://app:app@127.0.0.1:5433/veltrix"

    # MCP servers
    mcp_travel_url: str = "http://127.0.0.1:8101/mcp"
    mcp_email_url: str = "http://127.0.0.1:8102/mcp"
    mcp_calendar_url: str = "http://127.0.0.1:8103/mcp"
    mcp_timeout_seconds: float = 30.0

    # RAG
    rag_top_k: int = 4
    rag_max_distance: float = 0.55
    rag_max_context_chars: int = 6000
    chunk_size: int = 900
    chunk_overlap: int = 120

    # Orchestrator
    use_llm_followups: bool = True
    request_timeout_seconds: float = 300.0

    # Personal defaults
    home_city: str = "Kochi"
    home_currency: str = ""  # empty = derived from HOME_CITY (Kochi -> INR)
    manager_email: str = "manager@veltrix.io"
    user_name: str = "Your Name"

    # UI -> API
    api_url: str = "http://127.0.0.1:8000"

    @property
    def docs_dir(self) -> Path:
        return ROOT / "data" / "docs"


settings = Settings()
