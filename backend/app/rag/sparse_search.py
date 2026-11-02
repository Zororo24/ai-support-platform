import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document


def build_or_query(text: str) -> str:
    """Turn free text into an OR tsquery string ('a | b | c').

    OR (not AND) because natural-language questions rarely contain every term of the
    answer; ts_rank then orders chunks by how many terms they match.
    """
    tokens = dict.fromkeys(t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1)
    return " | ".join(tokens)


def sparse_search(
    db: Session,
    *,
    tenant_id: UUID,
    query: str,
    limit: int = 20,
    collection_ids: list[UUID] | None = None,
) -> list[dict[str, Any]]:
    if collection_ids is not None and not collection_ids:
        return []
    tsq_text = build_or_query(query)
    if not tsq_text:
        return []
    tsquery = func.to_tsquery("english", tsq_text)
    rank = func.ts_rank(Chunk.tsv, tsquery)
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.context,
            Chunk.content_hash,
            Document.title,
            rank.label("rank"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.tenant_id == tenant_id, Document.tenant_id == tenant_id)
        .where(Chunk.tsv.op("@@")(tsquery))
        .where(*([Document.collection_id.in_(collection_ids)] if collection_ids is not None else []))
        .order_by(rank.desc())
        .limit(limit)
    )
    return [
        {
            "chunk_id": str(row.id),
            "document_id": str(row.document_id),
            "title": row.title,
            "content": row.content,
            "context": row.context,
            "content_hash": row.content_hash,
            "score": float(row.rank),
        }
        for row in db.execute(stmt).all()
    ]
