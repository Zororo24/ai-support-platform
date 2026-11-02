from typing import Any

RRF_K = 8


def rrf_fuse(*ranked_lists: list[dict[str, Any]], k: int = RRF_K, limit: int = 5) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion: score(chunk) = sum over lists of 1 / (k + rank), rank from 1."""
    scores: dict[str, float] = {}
    first_seen: dict[str, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            chunk_id = item["chunk_id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(chunk_id, item)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:limit]
    return [{**first_seen[cid], "score": scores[cid]} for cid in ordered]
