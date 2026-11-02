import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.rag import cache
from app.rag.embeddings import embed_texts
from app.rag.hybrid import rrf_fuse
from app.rag.rerank import rerank_chunks
from app.rag.sparse_search import sparse_search
from app.rag.vector_search import MIN_SIMILARITY, vector_search

logger = logging.getLogger(__name__)

CANDIDATES = 20


def retrieve_dense(
    db: Session,
    *,
    tenant_id: UUID,
    query: str,
    limit: int = 5,
    collection_ids: list[UUID] | None = None,
    min_similarity: float = MIN_SIMILARITY,
) -> list[dict[str, Any]]:
    [embedding] = embed_texts([query])
    return vector_search(
        db,
        tenant_id=tenant_id,
        query_embedding=embedding,
        limit=limit,
        collection_ids=collection_ids,
        min_similarity=min_similarity,
    )


def scope_key(
    collection_ids: list[UUID] | None, min_similarity: float, limit: int, rerank: bool = False
) -> str:
    ids = "all" if collection_ids is None else ",".join(sorted(str(c) for c in collection_ids))
    return f"c={ids};s={min_similarity};k={limit}" + (";r=1" if rerank else "")


def rerank_pool(limit: int) -> int:
    """How many fused candidates the reranker reads to choose `limit` of them."""
    return min(CANDIDATES, max(limit * 3, 10))


def retrieve(
    db: Session,
    *,
    tenant_id: UUID,
    query: str,
    limit: int = 5,
    collection_ids: list[UUID] | None = None,
    min_similarity: float = MIN_SIMILARITY,
    rerank: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Hybrid (dense + FTS, fused with RRF) retrieval, cached per tenant and scope.

    `collection_ids=None` means every document of the tenant; a list restricts to those
    collections (an empty list finds nothing). Returns (chunks, cache_hit). A cache hit
    performs no database work.
    """
    if collection_ids is not None and not collection_ids:
        return [], False
    scope = scope_key(collection_ids, min_similarity, limit, rerank)
    cached = cache.get_cached(tenant_id, query, scope)
    if cached is not None:
        logger.info("retrieve cache_hit=true tenant=%s", tenant_id)
        return cached, True

    dense = retrieve_dense(
        db,
        tenant_id=tenant_id,
        query=query,
        limit=CANDIDATES,
        collection_ids=collection_ids,
        min_similarity=min_similarity,
    )
    sparse = sparse_search(
        db, tenant_id=tenant_id, query=query, limit=CANDIDATES, collection_ids=collection_ids
    )
    if rerank:
        chunks = rerank_chunks(query, rrf_fuse(dense, sparse, limit=rerank_pool(limit)), limit)
    else:
        chunks = rrf_fuse(dense, sparse, limit=limit)
    cache.set_cached(tenant_id, query, chunks, scope)
    logger.info("retrieve cache_hit=false tenant=%s chunks=%d", tenant_id, len(chunks))
    return chunks, False
