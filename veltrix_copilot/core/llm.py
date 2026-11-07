"""The only module that talks to Ollama: chat, structured (JSON) output and embeddings."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, TypeVar

from ollama import Client, ResponseError
from pydantic import BaseModel, ValidationError

from veltrix_copilot.core.config import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
_client: Client | None = None
_EMBED_PREFIX = {"document": "search_document: ", "query": "search_query: "}   # nomic-embed-text task prefixes
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """Ollama cannot be reached or the model is missing."""


def client() -> Client:
    global _client
    if _client is None:
        _client = Client(host=settings.ollama_url, timeout=settings.llm_timeout_seconds)
    return _client


def _fit_context(messages: list[dict], num_predict: int) -> list[dict]:
    """Trim the last user message (where documents live) so the prompt fits num_ctx; never the system prompt."""
    overflow = sum(len(m.get("content", "")) for m in messages) - int((settings.num_ctx - num_predict - 200) * 3.5)
    if overflow <= 0:
        return messages
    trimmed = [dict(m) for m in messages]
    for m in reversed(trimmed):
        if m.get("role") == "user" and len(m["content"]) > overflow + 100:
            m["content"] = m["content"][: len(m["content"]) - overflow - 40] + "\n[...truncated to fit context...]"
            log.warning("Prompt trimmed by %d chars to fit num_ctx=%d", overflow, settings.num_ctx)
            break
    return trimmed


def _no_think(messages: list[dict], model: str) -> list[dict]:
    """Qwen3's '/no_think' soft switch, in case the API's think=False is ignored."""
    if not settings.disable_thinking or "qwen" not in model.lower():
        return messages
    msgs = [dict(m) for m in messages]
    system = next((m for m in msgs if m.get("role") == "system"), None)
    if system is None:
        return [{"role": "system", "content": "/no_think"}, *msgs]
    system["content"] += "\n/no_think"
    return msgs


def strip_thinking(text: str) -> str:
    """Remove <think> blocks: complete, missing their opening tag, or cut off."""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.split("<think>", 1)[0].strip()


def chat(messages: list[dict], *, model: str | None = None, num_predict: int = 512, fmt: dict | None = None) -> str:
    """One completion at temperature 0. Returns the answer text only, never reasoning."""
    model = model or settings.chat_model
    kwargs: dict[str, Any] = {
        "model": model, "messages": _no_think(_fit_context(messages, num_predict), model),
        "options": {"temperature": 0, "num_ctx": settings.num_ctx, "num_predict": num_predict},
        "keep_alive": settings.keep_alive,
    }
    if fmt is not None:
        kwargs["format"] = fmt                      # JSON schema: Ollama constrains the output to it
    if settings.disable_thinking:
        kwargs["think"] = False
    try:
        try:
            resp = client().chat(**kwargs)
        except ResponseError as e:                  # models without a thinking mode may reject `think`
            if "think" not in kwargs or "think" not in str(e).lower():
                raise
            kwargs.pop("think")
            resp = client().chat(**kwargs)
    except Exception as e:
        raise LLMUnavailable(f"Ollama error ({settings.ollama_url}): {e}") from e
    return strip_thinking(resp.message.content or "")


def extract_json(text: str) -> str:
    """The {...} object inside `text`, dropping any chatter around it."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def structured(schema: type[T], messages: list[dict], *, model: str | None = None, retries: int = 1,
               num_predict: int = 400) -> T | None:
    """JSON matching `schema` (Ollama-constrained, Pydantic-validated, one retry with the error). None on failure."""
    fmt, msgs = schema.model_json_schema(), list(messages)
    for attempt in range(retries + 1):
        text = chat(msgs, model=model, fmt=fmt, num_predict=num_predict)
        try:
            return schema.model_validate_json(extract_json(text))
        except ValidationError as e:
            log.info("Structured output invalid (attempt %d): %s", attempt + 1, str(e)[:200])
            msgs += [{"role": "assistant", "content": text[:2000]},
                     {"role": "user", "content": f"That JSON was invalid: {str(e)[:300]}. Return only valid JSON for the schema."}]
    return None


async def astructured(schema: type[T], messages: list[dict], **kw) -> T | None:
    return await asyncio.to_thread(structured, schema, messages, **kw)


def embed(texts: list[str], kind: str = "document", batch_size: int = 16) -> list[list[float]]:
    """Embed on the CPU (num_gpu 0) so the chat model keeps the GPU. kind: 'document' or 'query'."""
    prefix = _EMBED_PREFIX.get(kind, "")
    options = {"num_gpu": 0} if settings.embed_on_cpu else None
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        try:
            resp = client().embed(model=settings.embed_model, input=[prefix + t for t in texts[i:i + batch_size]],
                                  options=options, keep_alive=settings.keep_alive)
        except Exception as e:
            raise LLMUnavailable(f"Embedding failed ({settings.embed_model}): {e}") from e
        vectors.extend(list(v) for v in resp.embeddings)
    if vectors and len(vectors[0]) != settings.embed_dim:
        raise ValueError(f"Embedding model returned {len(vectors[0])} dims but EMBED_DIM={settings.embed_dim}. "
                         "Fix EMBED_DIM in .env and re-run ingestion with --reset.")
    return vectors


def status() -> dict:
    """Are the models pulled, and how much of each loaded model is on the GPU?"""
    out: dict[str, Any] = {"ok": False, "url": settings.ollama_url}
    try:
        models = {m.model for m in client().list().models}
        out["chat_model_pulled"] = any(m.startswith(settings.chat_model) for m in models)
        out["embed_model_pulled"] = any(m.startswith(settings.embed_model) for m in models)
        out["loaded"] = [{"model": m.model, "gpu_percent": round(100 * (m.size_vram or 0) / m.size) if m.size else 0}
                         for m in client().ps().models]
        out["ok"] = out["chat_model_pulled"] and out["embed_model_pulled"]
    except Exception as e:
        out["error"] = str(e)
    return out
