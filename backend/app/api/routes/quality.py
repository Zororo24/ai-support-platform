"""Retrieval quality for a collection: test questions and the report that measures them."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.llm import LLMUnavailable
from app.core.ratelimit import rate_limited
from app.db.models import Document, QualityReport, User, UserRole
from app.db.session import get_db
from app.services import audit, quality
from app.services import collections as collections_service

router = APIRouter(prefix="/admin", tags=["knowledge quality"])

admin_only = require_roles(UserRole.admin)
_throttle = rate_limited("retrieval", per_user="retrieval_test_rate_per_user")


class ProbeOut(BaseModel):
    id: UUID
    document_id: UUID
    document_title: str
    question: str
    source: str


class ProbeIn(BaseModel):
    document_id: UUID
    question: str = Field(min_length=5, max_length=300)


class GenerateIn(BaseModel):
    per_document: int = Field(default=3, ge=1, le=5)


class ReportRequest(BaseModel):
    k: int = Field(default=5, ge=1, le=10)
    rerank: bool = False


class ReportSummary(BaseModel):
    id: UUID
    k: int
    rerank: bool
    probe_count: int
    doc_recall: float
    chunk_recall: float
    mrr: float
    created_at: datetime


class ReportOut(ReportSummary):
    per_document: list[dict[str, Any]]
    misses: list[dict[str, Any]]
    issues: list[dict[str, Any]]


class QualityOut(BaseModel):
    latest: ReportOut | None
    previous: ReportSummary | None
    probe_count: int
    document_count: int


def _summary(r: QualityReport) -> ReportSummary:
    return ReportSummary(
        id=r.id,
        k=r.k,
        rerank=r.rerank,
        probe_count=r.probe_count,
        doc_recall=r.doc_recall,
        chunk_recall=r.chunk_recall,
        mrr=r.mrr,
        created_at=r.created_at,
    )


def _report(r: QualityReport) -> ReportOut:
    return ReportOut(
        **_summary(r).model_dump(), per_document=r.per_document, misses=r.misses, issues=r.issues
    )


def _collection(db: Session, user: User, collection_id: UUID):
    found = collections_service.get_collection(db, tenant_id=user.tenant_id, collection_id=collection_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return found


def _error(exc: quality.QualityError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/collections/{collection_id}/quality", response_model=QualityOut)
def get_quality(
    collection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> QualityOut:
    collection = _collection(db, user, collection_id)
    reports = quality.list_reports(db, tenant_id=user.tenant_id, collection_id=collection.id, limit=2)
    documents = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.tenant_id == user.tenant_id, Document.collection_id == collection.id)
        )
        or 0
    )
    return QualityOut(
        latest=_report(reports[0]) if reports else None,
        previous=_summary(reports[1]) if len(reports) > 1 else None,
        probe_count=len(quality.list_probes(db, tenant_id=user.tenant_id, collection_id=collection.id)),
        document_count=documents,
    )


@router.post("/collections/{collection_id}/quality", response_model=QualityOut)
def run_quality_report(
    collection_id: UUID,
    body: ReportRequest,
    user: User = Depends(admin_only),
    _limit: None = Depends(_throttle),
    db: Session = Depends(get_db),
) -> QualityOut:
    """Search with every test question and measure how often the right document comes back."""
    collection = _collection(db, user, collection_id)
    try:
        quality.run_report(
            db, tenant_id=user.tenant_id, collection=collection, k=body.k, rerank=body.rerank, user=user
        )
    except quality.QualityError as exc:
        raise _error(exc)
    except LLMUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The AI service is unavailable."
        )
    return get_quality(collection_id, user, db)


@router.get("/collections/{collection_id}/quality/history", response_model=list[ReportSummary])
def quality_history(
    collection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[ReportSummary]:
    collection = _collection(db, user, collection_id)
    return [
        _summary(r) for r in quality.list_reports(db, tenant_id=user.tenant_id, collection_id=collection.id)
    ]


@router.get("/collections/{collection_id}/probes", response_model=list[ProbeOut])
def list_probes(
    collection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[ProbeOut]:
    collection = _collection(db, user, collection_id)
    return [
        ProbeOut(**p) for p in quality.list_probes(db, tenant_id=user.tenant_id, collection_id=collection.id)
    ]


@router.post("/collections/{collection_id}/probes/generate")
def generate_probes(
    collection_id: UUID,
    body: GenerateIn,
    user: User = Depends(admin_only),
    _limit: None = Depends(_throttle),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Have the AI write test questions from the documents' own passages."""
    collection = _collection(db, user, collection_id)
    try:
        result = quality.generate_probes(
            db, tenant_id=user.tenant_id, collection=collection, per_document=body.per_document
        )
    except LLMUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The AI service is unavailable."
        )
    audit.record(
        db, actor=user, action="quality.generate_probes", target_type="collection", target_id=collection.id,
        detail=result,
    )  # fmt: skip
    return result


@router.post(
    "/collections/{collection_id}/probes", response_model=ProbeOut, status_code=status.HTTP_201_CREATED
)
def add_probe(
    collection_id: UUID, body: ProbeIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> ProbeOut:
    collection = _collection(db, user, collection_id)
    try:
        probe = quality.add_probe(
            db,
            tenant_id=user.tenant_id,
            collection=collection,
            document_id=body.document_id,
            question=body.question,
        )
    except quality.QualityError as exc:
        raise _error(exc)
    title = next(
        p["document_title"]
        for p in quality.list_probes(db, tenant_id=user.tenant_id, collection_id=collection.id)
        if p["id"] == probe.id
    )
    return ProbeOut(
        id=probe.id,
        document_id=probe.document_id,
        document_title=title,
        question=probe.question,
        source=probe.source,
    )


@router.delete("/probes/{probe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_probe(probe_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    if not quality.delete_probe(db, tenant_id=user.tenant_id, probe_id=probe_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test question not found")


@router.post("/collections/{collection_id}/reindex")
def reindex_collection(
    collection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> dict[str, int]:
    """Re-index every document in the collection (passages that did not change are not re-embedded)."""
    collection = _collection(db, user, collection_id)
    queued = quality.reindex_collection(db, tenant_id=user.tenant_id, collection=collection)
    audit.record(
        db, actor=user, action="collection.reindex", target_type="collection", target_id=collection.id,
        detail={"documents": queued},
    )  # fmt: skip
    return {"documents": queued}
