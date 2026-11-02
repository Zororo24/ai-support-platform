from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core import llm
from app.core.llm import LLMUnavailable
from app.rag.embeddings import embed_texts

REAL_POST = llm._post  # captured at import, before the autouse fake is installed


@pytest.fixture
def real_post(monkeypatch):
    """Run the real HTTP client logic (httpx itself is mocked per test)."""
    from app.core.config import get_settings

    monkeypatch.setattr(llm, "_post", REAL_POST)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    yield llm
    monkeypatch.undo()
    get_settings.cache_clear()


def response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.raise_for_status.side_effect = (
        None if status < 400 else httpx.HTTPStatusError("err", request=MagicMock(), response=r)
    )
    return r


def test_missing_key_raises(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(llm, "_post", REAL_POST)
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMUnavailable, match="LLM_API_KEY"):
            llm.chat_completion([{"role": "user", "content": "hi"}])
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_retries_rate_limit_then_succeeds(real_post) -> None:
    ok = response(200, {"choices": [{"message": {"content": "hello"}}]})
    with patch.object(llm.httpx, "post", side_effect=[response(429), response(503), ok]) as post:
        assert llm.chat_completion([{"role": "user", "content": "hi"}]) == "hello"
    assert post.call_count == 3


def test_gives_up_after_max_attempts(real_post) -> None:
    with patch.object(llm.httpx, "post", return_value=response(429)) as post:
        with pytest.raises(LLMUnavailable):
            llm.chat_completion([{"role": "user", "content": "hi"}])
    assert post.call_count == llm.MAX_ATTEMPTS


def test_client_error_is_not_retried(real_post) -> None:
    with patch.object(llm.httpx, "post", return_value=response(401)) as post:
        with pytest.raises(LLMUnavailable):
            llm.chat_completion([{"role": "user", "content": "hi"}])
    assert post.call_count == 1


def test_json_fenced_output_is_parsed(real_post) -> None:
    fenced = response(200, {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]})
    with patch.object(llm.httpx, "post", return_value=fenced):
        assert llm.chat_json([{"role": "user", "content": "x"}]) == {"a": 1}


def test_embeddings_are_normalised_batched_and_dimensioned(fake_llm) -> None:
    sizes = []

    def three_four(path, payload):
        sizes.append(len(payload["input"]))
        assert payload["dimensions"] == 1536
        return {"data": [{"index": i, "embedding": [3.0, 4.0]} for i in range(len(payload["input"]))]}

    fake_llm.override = three_four
    vectors = embed_texts([f"t{i}" for i in range(120)])
    assert sizes == [50, 50, 20]
    assert len(vectors) == 120
    assert vectors[0] == pytest.approx([0.6, 0.8])  # unit length after normalising


def test_embedding_dimension_mismatch_fails_fast(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="1536"):
            embed_texts(["x"])
    finally:
        monkeypatch.delenv("EMBEDDING_DIMENSIONS")
        get_settings.cache_clear()
