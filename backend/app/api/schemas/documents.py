from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import DocumentStatus


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=500_000)
    source: str = Field(default="paste", max_length=300)
    collection_id: UUID | None = None  # default collection when omitted


class DocumentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=500_000)
    collection_id: UUID | None = None  # move to another collection (only applied when sent)


class DocumentOut(BaseModel):
    id: UUID
    title: str
    source: str
    status: DocumentStatus
    error: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int = 0
    chunk_count: int = 0
    collection_id: UUID | None = None
    collection_name: str | None = None
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class ChunkOut(BaseModel):
    index: int
    content: str


class DocumentDetail(DocumentOut):
    content: str
    chunks: list[ChunkOut]


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int


class RetrievalTestIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    agent_id: UUID | None = None  # use this agent's knowledge settings instead of the whole workspace
    rerank: bool | None = None  # let the model re-order the best candidates (default: the agent's setting)


class RetrievalHit(BaseModel):
    document_id: UUID
    title: str
    content: str
    score: float
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    section: str | None = None  # the heading path the passage sits under
    rerank_score: float | None = None


class RetrievalTestOut(BaseModel):
    query: str
    cache_hit: bool  # would a real chat query for this text be served from cache right now
    reranked: bool = False
    hits: list[RetrievalHit]
