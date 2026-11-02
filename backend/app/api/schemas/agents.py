from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    template: Literal["support", "blank"] = "support"


class AgentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class AgentSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str
    published_version: int | None
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime


class AgentDetail(AgentSummary):
    draft_config: dict[str, Any]


class PublishIn(BaseModel):
    note: str = Field(default="", max_length=300)
    # Publishing runs the agent's tests first. Skipping them needs a reason, which is audited.
    skip_tests: bool = False
    skip_reason: str = Field(default="", max_length=300)


class VersionOut(BaseModel):
    version: int
    note: str
    created_at: datetime
    created_by_email: str | None = None
    is_live: bool
    # What the publish gate found: {passed, failed, skipped, override_reason}
    tests: dict[str, Any] | None = None


class VersionDetail(VersionOut):
    config: dict[str, Any]


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Only these addresses or ranges may use the key; empty = anywhere.
    allowed_ips: list[str] = Field(default_factory=list, max_length=20)


class KeyIpsIn(BaseModel):
    allowed_ips: list[str] = Field(max_length=20)


class KeyOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    allowed_ips: list[str] = Field(default_factory=list)


class KeyCreated(KeyOut):
    key: str  # the only time the full secret is ever returned


class PlaygroundIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=200)
    version: Literal["draft", "published"] = "draft"
    end_user_attributes: dict[str, Any] = Field(default_factory=dict)
    mock_actions: bool = False
    workflow: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,39}$")


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class CollectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class CollectionOut(BaseModel):
    id: UUID
    name: str
    description: str
    is_default: bool
    document_count: int


class PlaygroundConfirmIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    confirmation_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirm", "cancel"]
    version: Literal["draft", "published"] = "draft"
    mock_actions: bool = False


class ActionTestIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)  # sample end-user attributes
    mock: bool = False
    send_for_real: bool = False


class ActionTestOut(BaseModel):
    refused: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    model_view: dict[str, Any] | None = None
