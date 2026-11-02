from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document

# Dense hits below this cosine similarity are noise, not evidence.
MIN_SIMILARITY = 0.1


def vector_search(
    db: Session,
    *,
    tenant_id: UUID,
    query_embedding: list[float],
    limit: int = 20,
    collection_ids: list[UUID] | None = None,
    min_similarity: float = MIN_SIMILARITY,
) -> list[dict[str, Any]]:
    """`collection_ids=None` searches the whole tenant; an empty list searches nothing."""
    if collection_ids is not None and not collection_ids:
        return []
    distance = Chunk.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.context,
            Chunk.content_hash,
            Document.title,
            distance.label("distance"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.tenant_id == tenant_id, Document.tenant_id == tenant_id)
        .where(Chunk.embedding.is_not(None))
        .where(*([Document.collection_id.in_(collection_ids)] if collection_ids is not None else []))
        .order_by(distance)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "chunk_id": str(row.id),
            "document_id": str(row.document_id),
            "title": row.title,
            "content": row.content,
            "context": row.context,
            "content_hash": row.content_hash,
            "score": 1.0 - float(row.distance),
        }
        for row in rows
        if 1.0 - float(row.distance) >= min_similarity
    ]
