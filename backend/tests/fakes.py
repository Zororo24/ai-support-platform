"""Deterministic stand-in for the LLM HTTP layer (`app.core.llm._post`).

The rules below exist ONLY to make tests reproducible; production code has no rule-based path.
"""

import hashlib
import json
import math
import re
from typing import Any

TICKET_RE = re.compile(
    r"\bticket\b|\bexception\b|\bcomplain|\b(speak|talk)\s+(to|with)\s+(a\s+)?(human|person|agent|someone)\b|\bescalat",
    re.I,
)
ORDER_RE = re.compile(r"\b[A-Za-z]{3}-\d{3,}\b")
ORDER_WORDS_RE = re.compile(r"\b(order|tracking|package|shipment)\b", re.I)
ORDER_STATUS_RE = re.compile(r"\b(where|status|track|arriv|deliver)", re.I)
GREETING_RE = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|bye)\W*$", re.I)


def dummy_embedding(text: str, dim: int = 1536) -> list[float]:
    """Hashed bag-of-words: texts sharing words land close together. Not semantic."""
    vec = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "big") % dim] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def classify_text(text: str) -> tuple[str, float]:
    if text.strip().lower() == "hmm":
        return "knowledge", 0.3  # deliberately ambiguous
    if GREETING_RE.match(text):
        return "other", 0.95
    if TICKET_RE.search(text):
        return "ticket", 0.95
    if ORDER_RE.search(text) or (ORDER_WORDS_RE.search(text) and ORDER_STATUS_RE.search(text)):
        return "order", 0.95
    return "knowledge", 0.9


class FakeLLM:
    def __init__(self) -> None:
        self.chat_calls: list[list[dict[str, str]]] = []
        self.embed_calls = 0
        self.override: Any = None  # callable(path, payload) -> dict, for per-test behaviour
        self.decisions: list[dict[str, Any]] = []  # scripted "decide the next step" answers, in order
        self.extractions: list[dict[str, Any]] = []  # scripted form-filling answers, in order
        self.writings: list[dict[str, Any]] = []  # scripted workflow "write a text" answers
        self.embedded_texts = 0  # how many individual texts were embedded (not requests)
        self.rewrites: list[str] = []  # scripted standalone queries; otherwise the message is repeated
        self.rewrite_calls = 0
        self.rerank_scores: list[dict[str, Any]] = []  # scripted rerank answers; otherwise word overlap
        self.rerank_calls = 0
        self.probe_questions: list[dict[str, Any]] = []  # scripted test-question answers

    def __call__(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.override is not None:
            return self.override(path, payload)
        if path == "/embeddings":
            self.embed_calls += 1
            self.embedded_texts += len(payload["input"])
            dim = payload.get("dimensions", 1536)
            return {
                "data": [
                    {"index": i, "embedding": dummy_embedding(t, dim)} for i, t in enumerate(payload["input"])
                ],
                "usage": {"total_tokens": 5},
            }
        assert path == "/chat/completions", path
        self.chat_calls.append(payload["messages"])
        return {
            "choices": [{"message": {"content": json.dumps(self._chat(payload["messages"]))}}],
            "usage": {"total_tokens": 15},
        }

    def script(self, *decisions: dict[str, Any]) -> None:
        """Queue the exact decisions the agent loop's model will make, one per decide call."""
        self.decisions.extend(decisions)

    def extract(self, values: dict[str, Any] | None = None, intent: str = "answer") -> None:
        """Queue what the form-filling step will extract from the customer's next message."""
        self.extractions.append({"values": values or {}, "intent": intent})

    def write(self, **keys: Any) -> None:
        self.writings.append(keys)

    def decide_prompts(self) -> list[list[dict[str, str]]]:
        return [c for c in self.chat_calls if "decide the next step" in c[0]["content"]]

    def _chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system, user = messages[0]["content"], messages[-1]["content"]
        if "decide the next step" in system:
            if not self.decisions:
                raise AssertionError("the agent loop asked for a decision but none was scripted")
            return self.decisions.pop(0)
        if "extract details from a customer's message" in system:
            if not self.extractions:
                raise AssertionError("the workflow asked to extract details but none were scripted")
            return self.extractions.pop(0)
        if "write a short text for a customer" in system:
            return self.writings.pop(0) if self.writings else {"text": "(written text)"}
        if "standalone search query" in system:
            self.rewrite_calls += 1
            if self.rewrites:
                return {"query": self.rewrites.pop(0)}
            return {"query": user.split("Latest message:", 1)[1].strip()}
        if "rank search results" in system:
            self.rerank_calls += 1
            if self.rerank_scores:
                return self.rerank_scores.pop(0)
            question = user.rsplit("Question:", 1)[1].strip().lower()
            words = set(re.findall(r"[a-z0-9]+", question))
            passages = re.findall(r"\[(\d+)\] \(.*?\)\n(.*?)(?=\n\n\[\d+\] \(|\n\nQuestion:)", user, re.S)
            return {
                "scores": [
                    {"passage": int(n), "score": len(words & set(re.findall(r"[a-z0-9]+", body.lower())))}
                    for n, body in passages
                ]
            }
        if "write test questions" in system:
            if self.probe_questions:
                return self.probe_questions.pop(0)
            passages = re.findall(r"\[(\d+)\] \(.*?\)\n(.*?)(?=\n\n\[\d+\] \(|\Z)", user, re.S)
            return {
                "questions": [
                    {"passage": int(n), "question": "Tell me about " + " ".join(body.split()[:7])}
                    for n, body in passages
                ]
            }
        if "classify a customer's answer" in system:
            answer = user.split("Customer's answer:", 1)[1].strip().lower()
            if re.match(r"^(yes|y|yeah|yep|sure|ok|okay|go ahead|please do|do it)\b", answer):
                return {"decision": "confirm"}
            if re.match(r"^(no|nope|nah|cancel|stop|don't|never mind)\b", answer):
                return {"decision": "cancel"}
            return {"decision": "other"}
        if "route messages" in system:
            latest = user.split("Latest message:", 1)[1].strip()
            intent, confidence = classify_text(latest)
            return {"intent": intent, "confidence": confidence}
        if "Write a support ticket" in system:
            transcript = user.split("Conversation:\n", 1)[1]
            last_user = [ln for ln in transcript.splitlines() if ln.startswith("user:")][-1][5:].strip()
            return {"subject": last_user[:80], "body": f"Customer reported: {last_user}"}
        if "numbered context passages" in system:
            body = user.split("Context passages:\n", 1)[1].rsplit("\n\nQuestion:", 1)[0]
            passages = re.findall(r"\[(\d+)\] \(.*?\)\n(.*?)(?=\n\n\[\d+\] \(|\Z)", body, re.S)
            first = " ".join(passages[0][1].split())
            return {"supported": True, "answer": first, "sources": [1]}
        raise AssertionError(f"FakeLLM got an unrecognised prompt: {system[:80]!r}")
