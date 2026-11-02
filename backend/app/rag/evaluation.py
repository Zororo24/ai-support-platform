"""Measure retrieval against questions with a known right document.

Used by the retrieval-quality test (a fixed golden set, so a change that makes search worse fails
CI) and by `scripts/eval_retrieval.py` (run against a real model before a release). A question
counts as found at rank r when a passage of the expected document is the r-th result.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.rag.embeddings import embed_texts
from app.rag.hybrid import rrf_fuse
from app.rag.rerank import rerank_chunks
from app.rag.retrieve import CANDIDATES, rerank_pool
from app.rag.sparse_search import sparse_search
from app.rag.vector_search import MIN_SIMILARITY, vector_search

K_VALUES = (1, 3, 5)


def evaluate(
    db: Session,
    *,
    tenant_id: UUID,
    cases: list[dict[str, str]],
    collection_ids: list[UUID] | None = None,
    mode: str = "hybrid",  # hybrid | dense | sparse
    rerank: bool = False,
) -> dict[str, Any]:
    """`cases` are {"q": question, "expect": document title}. Returns recall@k, MRR and the misses."""
    vectors = embed_texts([c["q"] for c in cases]) if mode != "sparse" else [[] for _ in cases]
    ranks: list[int | None] = []
    misses: list[dict[str, Any]] = []
    for case, vector in zip(cases, vectors, strict=True):
        dense = (
            vector_search(
                db,
                tenant_id=tenant_id,
                query_embedding=vector,
                limit=CANDIDATES,
                collection_ids=collection_ids,
                min_similarity=MIN_SIMILARITY,
            )
            if mode != "sparse"
            else []
        )
        sparse = (
            sparse_search(
                db, tenant_id=tenant_id, query=case["q"], limit=CANDIDATES, collection_ids=collection_ids
            )
            if mode != "dense"
            else []
        )
        top = max(K_VALUES)
        found = rrf_fuse(dense, sparse, limit=rerank_pool(top) if rerank else top)
        if rerank:
            found = rerank_chunks(case["q"], found, top)
        rank = next((i for i, c in enumerate(found, 1) if c["title"] == case["expect"]), None)
        ranks.append(rank)
        if rank is None:
            misses.append({"q": case["q"], "expect": case["expect"], "got": [c["title"] for c in found[:3]]})
    total = len(cases)
    return {
        "cases": total,
        "mode": mode,
        "rerank": rerank,
        "recall": {
            str(k): round(sum(1 for r in ranks if r is not None and r <= k) / total, 4) for k in K_VALUES
        },
        "mrr": round(sum(1.0 / r for r in ranks if r) / total, 4),
        "misses": misses,
    }
