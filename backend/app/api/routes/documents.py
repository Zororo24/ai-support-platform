import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.config import parse_config
from app.api.deps import require_roles
from app.api.schemas.documents import (
    ChunkOut,
    DocumentDetail,
    DocumentIn,
    DocumentList,
    DocumentOut,
    DocumentPatch,
    RetrievalHit,
    RetrievalTestIn,
    RetrievalTestOut,
)
from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from app.core.ratelimit import rate_limited
from app.db.models import Collection, Document, DocumentStatus, User, UserRole
from app.db.session import get_db
from app.rag import cache
from app.rag.hybrid import rrf_fuse
from app.rag.rerank import rerank_chunks
from app.rag.retrieve import CANDIDATES, rerank_pool, retrieve_dense, scope_key
from app.rag.sparse_search import sparse_search
from app.rag.vector_search import MIN_SIMILARITY
from app.services import agents as agents_service
from app.services import audit, jobs, tenant_llm
from app.services import documents as svc
from app.services.extract import ExtractionError, validate_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

admin_only = require_roles(UserRole.admin)


_ingest_throttle = rate_limited(
    "ingest", per_user="ingest_rate_per_user", per_tenant="ingest_rate_per_tenant"
)
_retrieval_throttle = rate_limited("retrieval", per_user="retrieval_test_rate_per_user")


def _ingest_limit(_: User = Depends(_ingest_throttle)) -> None:
    """Ingest costs embedding calls, so it is throttled (declared after the admin check)."""


def _retrieval_limit(_: User = Depends(_retrieval_throttle)) -> None:
    """The retrieval tester embeds every query, so it is throttled too."""


def _emails(db: Session, documents: list[Document]) -> dict[UUID, str]:
    ids = {d.created_by for d in documents if d.created_by}
    if not ids:
        return {}
    rows = db.execute(select(User.id, User.email).where(User.id.in_(ids))).all()
    return {row.id: row.email for row in rows}


def _collection_names(db: Session, tenant_id: UUID) -> dict[UUID, str]:
    rows = db.execute(select(Collection.id, Collection.name).where(Collection.tenant_id == tenant_id)).all()
    return {row.id: row.name for row in rows}


def _out(document: Document, emails: dict[UUID, str], names: dict[UUID, str]) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        title=document.title,
        source=document.source,
        status=document.status,
        error=document.error,
        file_name=document.file_name,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        chunk_count=document.chunk_count,
        collection_id=document.collection_id,
        collection_name=names.get(document.collection_id) if document.collection_id else None,
        created_by_email=emails.get(document.created_by) if document.created_by else None,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _single_out(db: Session, document: Document) -> DocumentOut:
    return _out(document, _emails(db, [document]), _collection_names(db, document.tenant_id))


def _require_document(db: Session, user: User, document_id: UUID) -> Document:
    document = svc.get_document(db, tenant_id=user.tenant_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


def _create_or_http_error(db: Session, **kwargs) -> Document:
    try:
        return svc.create_document(db, **kwargs)
    except svc.DuplicateDocument as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate of existing document '{exc.existing.title}'",
        )
    except svc.collections_service.CollectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except svc.DocumentLimitReached:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document limit reached ({get_settings().max_documents_per_tenant} per tenant)",
        )


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def create_document(
    body: DocumentIn,
    background: BackgroundTasks,
    user: User = Depends(admin_only),
    _limit: None = Depends(_ingest_limit),
    db: Session = Depends(get_db),
) -> DocumentOut:
    document = _create_or_http_error(
        db,
        user=user,
        title=body.title,
        source=body.source,
        content=body.content,
        collection_id=body.collection_id,
    )
    background.add_task(jobs.kick, svc.enqueue_index(db, document))
    audit.record(
        db,
        actor=user,
        action="document.create",
        target_type="document",
        target_id=document.id,
        detail={"title": document.title, "source": "paste"},
    )
    return _single_out(db, document)


@router.post("/documents/upload", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=300),
    collection_id: UUID | None = Form(default=None),
    user: User = Depends(admin_only),
    _limit: None = Depends(_ingest_limit),
    db: Session = Depends(get_db),
) -> DocumentOut:
    limit = get_settings().max_upload_bytes
    data = file.file.read(limit + 1)  # never buffer more than limit+1 bytes
    if len(data) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File is larger than {limit // (1024 * 1024)} MB",
        )
    file_name = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1][:300]
    try:
        mime = validate_upload(file_name, data)
    except ExtractionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    document = _create_or_http_error(
        db,
        user=user,
        title=(title or "").strip() or file_name.rsplit(".", 1)[0] or file_name,
        source="upload",
        file_data=data,
        file_name=file_name,
        mime_type=mime,
        collection_id=collection_id,
    )
    background.add_task(jobs.kick, svc.enqueue_index(db, document))
    audit.record(
        db,
        actor=user,
        action="document.upload",
        target_type="document",
        target_id=document.id,
        detail={"title": document.title, "file_name": file_name, "size_bytes": len(data)},
    )
    return _single_out(db, document)


@router.get("/documents", response_model=DocumentList)
def list_documents(
    q: str | None = Query(default=None, max_length=200),
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
    collection_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> DocumentList:
    documents, total = svc.list_documents(
        db,
        tenant_id=user.tenant_id,
        q=q,
        status=status_filter,
        collection_id=collection_id,
        limit=limit,
        offset=offset,
    )
    emails = _emails(db, documents)
    names = _collection_names(db, user.tenant_id)
    return DocumentList(items=[_out(d, emails, names) for d in documents], total=total)


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> DocumentDetail:
    document = _require_document(db, user, document_id)
    chunks = svc.list_chunks(db, tenant_id=user.tenant_id, document_id=document.id)
    base = _single_out(db, document)
    return DocumentDetail(
        **base.model_dump(),
        content=document.content,
        chunks=[ChunkOut(index=c.chunk_index, content=c.content) for c in chunks],
    )


@router.patch("/documents/{document_id}", response_model=DocumentOut)
def patch_document(
    document_id: UUID,
    body: DocumentPatch,
    background: BackgroundTasks,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> DocumentOut:
    document = _require_document(db, user, document_id)
    changed = [
        f
        for f in ("title", "content", "collection_id")
        if f in body.model_fields_set and (f == "collection_id" or getattr(body, f) is not None)
    ]
    try:
        reindex = svc.update_document(
            db,
            document=document,
            title=body.title,
            content=body.content,
            set_collection="collection_id" in body.model_fields_set,
            collection_id=body.collection_id,
        )
    except svc.collections_service.CollectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if reindex:
        background.add_task(jobs.kick, svc.enqueue_index(db, document))
    audit.record(
        db,
        actor=user,
        action="document.update",
        target_type="document",
        target_id=document.id,
        detail={"fields": changed},
    )
    return _single_out(db, document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> None:
    document = _require_document(db, user, document_id)
    title = document.title
    svc.delete_document(db, document=document)
    audit.record(
        db,
        actor=user,
        action="document.delete",
        target_type="document",
        target_id=document_id,
        detail={"title": title},
    )


@router.post("/documents/{document_id}/reindex", response_model=DocumentOut)
def reindex(
    document_id: UUID,
    background: BackgroundTasks,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> DocumentOut:
    document = _require_document(db, user, document_id)
    svc.mark_pending(db, document)
    background.add_task(jobs.kick, svc.enqueue_index(db, document))
    audit.record(
        db,
        actor=user,
        action="document.reindex",
        target_type="document",
        target_id=document.id,
        detail={"title": document.title},
    )
    return _single_out(db, document)


@router.post("/retrieval/test", response_model=RetrievalTestOut)
def retrieval_test(
    body: RetrievalTestIn,
    user: User = Depends(admin_only),
    _limit: None = Depends(_retrieval_limit),
    db: Session = Depends(get_db),
) -> RetrievalTestOut:
    """Show exactly what retrieval returns for a query: dense vs keyword ranks and the fused order.

    Without `agent_id` the whole workspace is searched. With it, the agent's draft knowledge
    settings apply (attached collections, top-k, minimum similarity), which is what the agent
    would actually see. The cache is bypassed (results are always fresh), but the response says
    whether a real query for this text would currently be served from it.
    """
    tenant_llm.bind(db, user.tenant_id)
    collection_ids = None
    min_similarity = MIN_SIMILARITY
    top_k = 5
    agent_rerank = False
    if body.agent_id is not None:
        agent = agents_service.get_agent(db, tenant_id=user.tenant_id, agent_id=body.agent_id)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        try:
            knowledge = parse_config(agent.draft_config).knowledge
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The draft is invalid"
            )
        collection_ids = list(knowledge.collection_ids)
        min_similarity, top_k = knowledge.min_similarity, knowledge.top_k
        agent_rerank = knowledge.rerank
    try:
        dense = retrieve_dense(
            db,
            tenant_id=user.tenant_id,
            query=body.query,
            limit=CANDIDATES,
            collection_ids=collection_ids,
            min_similarity=min_similarity,
        )
    except LLMUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The embedding service is unavailable. Please try again shortly.",
        )
    sparse = sparse_search(
        db, tenant_id=user.tenant_id, query=body.query, limit=CANDIDATES, collection_ids=collection_ids
    )
    dense_by_id = {c["chunk_id"]: (rank, c["score"]) for rank, c in enumerate(dense, 1)}
    sparse_by_id = {c["chunk_id"]: (rank, c["score"]) for rank, c in enumerate(sparse, 1)}
    rerank = agent_rerank if body.rerank is None else body.rerank
    shown = 10 if body.agent_id is None else top_k
    fused = rrf_fuse(dense, sparse, limit=rerank_pool(shown) if rerank else shown)
    if rerank:
        fused = rerank_chunks(body.query, fused, shown)
    hits = []
    for chunk in fused:
        d_rank, d_score = dense_by_id.get(chunk["chunk_id"], (None, None))
        s_rank, s_score = sparse_by_id.get(chunk["chunk_id"], (None, None))
        hits.append(
            RetrievalHit(
                document_id=UUID(chunk["document_id"]),
                title=chunk["title"],
                content=chunk["content"],
                score=chunk["score"],
                dense_rank=d_rank,
                dense_score=d_score,
                sparse_rank=s_rank,
                sparse_score=s_score,
                section=chunk.get("context") or None,
                rerank_score=chunk.get("rerank_score"),
            )
        )
    scope = scope_key(collection_ids, min_similarity, top_k, rerank)
    return RetrievalTestOut(
        query=body.query,
        cache_hit=cache.get_cached(user.tenant_id, body.query, scope) is not None,
        reranked=rerank,
        hits=hits,
    )
