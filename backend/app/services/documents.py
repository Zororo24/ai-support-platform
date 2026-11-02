import hashlib
import logging
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from app.db.models import Chunk, Document, DocumentStatus, User
from app.db.session import get_session_factory
from app.rag import cache
from app.rag.chunking import chunk_document
from app.rag.embeddings import embed_texts
from app.services import collections as collections_service
from app.services import jobs, tenant_llm
from app.services.extract import ExtractionError, extract_text

logger = logging.getLogger(__name__)


class DuplicateDocument(Exception):
    def __init__(self, existing: Document) -> None:
        super().__init__(f"Duplicate of '{existing.title}'")
        self.existing = existing


class DocumentLimitReached(Exception):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_document(db: Session, *, tenant_id: UUID, document_id: UUID) -> Document | None:
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != tenant_id:
        return None
    return document


def create_document(
    db: Session,
    *,
    user: User,
    title: str,
    source: str,
    content: str = "",
    file_data: bytes | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
    collection_id: UUID | None = None,
) -> Document:
    """Create a `pending` document. Indexing happens separately (background or inline)."""
    tenant_id = user.tenant_id
    count = db.scalar(select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id))
    if (count or 0) >= get_settings().max_documents_per_tenant:
        raise DocumentLimitReached
    if collection_id is None:
        collection_id = collections_service.default_collection(db, tenant_id=tenant_id).id
    elif collections_service.get_collection(db, tenant_id=tenant_id, collection_id=collection_id) is None:
        raise collections_service.CollectionError("Unknown collection")
    raw = file_data if file_data is not None else content.encode("utf-8")
    digest = _hash(raw)
    existing = db.scalars(
        select(Document).where(Document.tenant_id == tenant_id, Document.content_hash == digest)
    ).first()
    if existing is not None:
        raise DuplicateDocument(existing)
    document = Document(
        tenant_id=tenant_id,
        title=title,
        source=source,
        content=content,
        status=DocumentStatus.pending,
        file_name=file_name,
        mime_type=mime_type or ("text/markdown" if file_data is None else None),
        size_bytes=len(raw),
        content_hash=digest,
        created_by=user.id,
        collection_id=collection_id,
        file_data=file_data,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def _rebuild_chunks(db: Session, document: Document) -> int:
    """Chunk the document and swap the new chunks in.

    A passage whose text (and heading path) is unchanged keeps its stored embedding, so editing one
    section of a long document costs one embedding call, not one per passage.
    """
    pieces = chunk_document(document.title, document.content)
    if not pieces:
        raise ExtractionError("No indexable text in this document")
    known: dict[str, list[list[float]]] = {}
    for row in db.execute(
        select(Chunk.content_hash, Chunk.embedding).where(
            Chunk.document_id == document.id,
            Chunk.tenant_id == document.tenant_id,
            Chunk.content_hash.is_not(None),
            Chunk.embedding.is_not(None),
        )
    ):
        known.setdefault(row.content_hash, []).append([float(x) for x in row.embedding])
    vectors: list[list[float] | None] = []
    missing: list[int] = []
    for index, piece in enumerate(pieces):
        reusable = known.get(piece.digest)
        vectors.append(reusable.pop() if reusable else None)
        if vectors[-1] is None:
            missing.append(index)
    if missing:
        fresh = embed_texts([pieces[i].embed_text for i in missing])  # may raise; nothing has changed yet
        for index, vector in zip(missing, fresh, strict=True):
            vectors[index] = vector
    logger.info(
        "indexed document %s: %d passages, %d embedded, %d reused",
        document.id,
        len(pieces),
        len(missing),
        len(pieces) - len(missing),
    )
    db.execute(delete(Chunk).where(Chunk.document_id == document.id, Chunk.tenant_id == document.tenant_id))
    for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True)):
        db.add(
            Chunk(
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=index,
                content=piece.content,
                context=piece.context,
                content_hash=piece.digest,
                embedding=vector,
            )
        )
    return len(pieces)


def _mark_failed(db: Session, document_id: UUID, message: str) -> None:
    db.rollback()
    document = db.get(Document, document_id)
    if document is not None:
        document.status = DocumentStatus.failed
        document.error = message[:500]
        db.commit()


def process_in_session(db: Session, document_id: UUID) -> None:
    """Extract (if needed), chunk, embed and swap in the new chunks atomically.

    On failure the previous chunks stay searchable and the document is marked `failed`.
    """
    document = db.get(Document, document_id)
    if document is None:
        return
    tenant_id = document.tenant_id
    tenant_llm.bind(db, tenant_id)
    document.status = DocumentStatus.processing
    document.error = None
    db.commit()
    try:
        if not document.content.strip():
            if document.file_data is None:
                raise ExtractionError("Nothing to index: the original file is no longer stored")
            document.content = extract_text(document.file_name or "", document.file_data)
        document.chunk_count = _rebuild_chunks(db, document)
        document.status = DocumentStatus.ready
        document.file_data = None
        db.commit()
    except ExtractionError as exc:
        _mark_failed(db, document_id, str(exc))
    except LLMUnavailable:
        logger.exception("embedding failed for document %s", document_id)
        _mark_failed(db, document_id, "The embedding service is unavailable. Retry in a few minutes.")
    except Exception:
        logger.exception("unexpected failure indexing document %s", document_id)
        _mark_failed(db, document_id, "Unexpected error while indexing this document.")
    finally:
        cache.invalidate_tenant(tenant_id)


def process_document(document_id: UUID) -> None:
    """Background-task entry point: uses its own session, never the request's."""
    with get_session_factory()() as db:
        process_in_session(db, document_id)


def enqueue_index(db: Session, document: Document) -> UUID:
    """Queue indexing for a document. Returns the job id (pass it to `jobs.kick` after responding)."""
    job = jobs.enqueue(
        db, type="document.index", payload={"document_id": str(document.id)}, tenant_id=document.tenant_id
    )
    return job.id


@jobs.handler("document.index")
def _index_job(db: Session, payload: dict) -> None:
    process_in_session(db, UUID(payload["document_id"]))


def ingest_document(db: Session, *, user: User, title: str, content: str, source: str = "upload") -> Document:
    """Create and index synchronously (seed script, tests)."""
    document = create_document(db, user=user, title=title, source=source, content=content)
    process_in_session(db, document.id)
    db.refresh(document)
    return document


def mark_pending(db: Session, document: Document) -> Document:
    document.status = DocumentStatus.pending
    document.error = None
    db.commit()
    db.refresh(document)
    return document


def list_documents(
    db: Session,
    *,
    tenant_id: UUID,
    q: str | None = None,
    status: DocumentStatus | None = None,
    collection_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Document], int]:
    conditions = [Document.tenant_id == tenant_id]
    if status is not None:
        conditions.append(Document.status == status)
    if collection_id is not None:
        conditions.append(Document.collection_id == collection_id)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(or_(Document.title.ilike(like), Document.file_name.ilike(like)))
    total = db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    stmt = (
        select(Document).where(*conditions).order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )
    return list(db.scalars(stmt)), total


def list_chunks(db: Session, *, tenant_id: UUID, document_id: UUID) -> list[Chunk]:
    stmt = (
        select(Chunk)
        .where(Chunk.tenant_id == tenant_id, Chunk.document_id == document_id)
        .order_by(Chunk.chunk_index)
    )
    return list(db.scalars(stmt))


def update_document(
    db: Session,
    *,
    document: Document,
    title: str | None,
    content: str | None,
    set_collection: bool = False,
    collection_id: UUID | None = None,
) -> bool:
    """Apply edits. Returns True when the title or content changed and a reindex must run."""
    reindex = False
    if title is not None and title != document.title:
        document.title = title
        # The title is the first part of every passage's heading path, so passages must be re-embedded.
        document.status = DocumentStatus.pending
        document.error = None
        reindex = True
    if set_collection:
        target = collection_id
        if target is None:
            target = collections_service.default_collection(db, tenant_id=document.tenant_id).id
        elif (
            collections_service.get_collection(db, tenant_id=document.tenant_id, collection_id=target) is None
        ):
            raise collections_service.CollectionError("Unknown collection")
        document.collection_id = target
    if content is not None and content != document.content:
        document.content = content
        document.content_hash = _hash(content.encode("utf-8"))
        document.size_bytes = len(content.encode("utf-8"))
        document.file_data = None
        document.status = DocumentStatus.pending
        document.error = None
        reindex = True
    db.commit()
    db.refresh(document)
    cache.invalidate_tenant(document.tenant_id)  # titles appear in cached citations too
    return reindex


def delete_document(db: Session, *, document: Document) -> None:
    tenant_id = document.tenant_id
    db.delete(document)  # chunks go with it (ORM cascade + FK ON DELETE CASCADE)
    db.commit()
    cache.invalidate_tenant(tenant_id)
