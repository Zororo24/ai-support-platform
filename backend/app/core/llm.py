"""OpenAI-compatible LLM client (chat + embeddings). Fails loudly, never degrades silently.

Every network call goes through `_post`, the single seam tests replace with a fake.
"""

import json
import logging
import time
from contextvars import ContextVar
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class LLMUnavailable(Exception):
    """The model could not be reached or returned something unusable."""


_usage: ContextVar[dict[str, int] | None] = ContextVar("llm_usage", default=None)


def new_usage(api_key: str | None = None) -> dict[str, Any]:
    """Counters for one turn. `api_key` is a workspace's own provider key, used instead of the platform's.

    It rides along with the counters because those are re-bound after every step of a streamed
    turn (which hops between threads), so the key follows the turn wherever it runs.
    """
    usage: dict[str, Any] = {"calls": 0, "total_tokens": 0}
    if api_key:
        usage["api_key"] = api_key
    return usage


def bind_usage(counters: dict[str, int]) -> None:
    """Make LLM calls on this context add to `counters`.

    Deliberately a plain `set` (no reset token): a streamed turn is resumed on different
    threads, each with its own copy of the context, so a token could not be reset elsewhere.
    """
    _usage.set(counters)


def _record_usage(data: dict[str, Any]) -> None:
    counters = _usage.get()
    if counters is None:
        return
    counters["calls"] += 1
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        counters["total_tokens"] += usage["total_tokens"]


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.llm_api_key and not (_usage.get() or {}).get("api_key"):
        raise LLMUnavailable("LLM_API_KEY is not configured")
    url = f"{settings.llm_base_url.rstrip('/')}{path}"
    api_key = (_usage.get() or {}).get("api_key") or settings.llm_api_key
    headers = {"Authorization": f"Bearer {api_key}"}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=settings.llm_timeout)
        except httpx.HTTPError as exc:
            last = exc
        else:
            if response.status_code not in RETRY_STATUS:
                try:
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise LLMUnavailable(f"LLM request failed: {exc}") from exc
            last = RuntimeError(f"HTTP {response.status_code}")
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2**attempt)  # free tiers rate-limit; back off 1s, 2s
    raise LLMUnavailable(f"LLM request failed after {MAX_ATTEMPTS} attempts: {last}")


def chat_completion(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    payload: dict[str, Any] = {
        "model": get_settings().llm_model,
        "messages": messages,
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = _post("/chat/completions", payload)
    _record_usage(data)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable("Unexpected LLM response shape") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMUnavailable("Empty LLM response")
    return content


def chat_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Chat completion parsed as a JSON object; one retry on malformed output."""
    for attempt in range(2):
        raw = chat_completion(messages, json_mode=True)
        text = raw.strip()
        if text.startswith("```"):  # some models fence JSON even in JSON mode
            text = text.strip("`").removeprefix("json").strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            logger.warning("LLM returned invalid JSON (attempt %d)", attempt + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMUnavailable("LLM did not return a JSON object")


def embed(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    data = _post(
        "/embeddings",
        {
            "model": settings.embedding_model,
            "input": texts,
            "dimensions": settings.embedding_dimensions,
        },
    )
    _record_usage(data)
    try:
        items = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]
    except (KeyError, TypeError) as exc:
        raise LLMUnavailable("Unexpected embeddings response shape") from exc
