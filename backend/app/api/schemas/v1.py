"""Request/response models of the public /v1 API."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,200}$"
MAX_ATTRIBUTES_BYTES = 4096
MAX_RESULT_BYTES = 65536


class EndUserIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_ATTRIBUTES_BYTES:
            raise ValueError(f"attributes must be under {MAX_ATTRIBUTES_BYTES} bytes")
        return value


class AgentMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)
    end_user: EndUserIn | None = None
    stream: bool = False
    # Start this guided process instead of letting the agent decide (e.g. a "Start a return" button).
    workflow: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,39}$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfirmationIn(BaseModel):
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    confirmation_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirm", "cancel"]


class ToolResultIn(BaseModel):
    """The outcome of an action your backend performed for the agent (`action_request`)."""

    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    call_id: str = Field(min_length=1, max_length=64)
    result: Any = None  # what your system returned; ignored when `error` is set
    error: str | None = Field(default=None, max_length=500)

    @field_validator("result")
    @classmethod
    def _small(cls, value: Any) -> Any:
        if len(json.dumps(value, default=str)) > MAX_RESULT_BYTES:
            raise ValueError(f"result must be under {MAX_RESULT_BYTES} bytes")
        return value


class CitationOut(BaseModel):
    document_id: str
    title: str
    snippet: str


class UsageOut(BaseModel):
    tokens: int = 0
    latency_ms: int = 0


class AgentMessageOut(BaseModel):
    run_id: str | None = None
    session_id: str
    message_id: str | None = None
    status: Literal[
        "completed",
        "needs_input",
        "needs_confirmation",
        "pending_approval",
        "action_required",
        "handed_off",
        "failed",
    ] = "completed"
    outcome: str = "answered"  # answered | refused | ... (more arrive with actions/workflows)
    reply: str
    citations: list[CitationOut] = Field(default_factory=list)
    actions_taken: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None
    action_request: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    usage: UsageOut = Field(default_factory=UsageOut)


class SessionStateOut(BaseModel):
    session_id: str
    end_user_id: str
    created_at: datetime
    last_active_at: datetime
    message_count: int
    active_run: dict[str, Any] | None = None
