from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.ratelimit import rate_limited
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import runs as svc

router = APIRouter(prefix="/admin", tags=["runs"])

admin_only = require_roles(UserRole.admin)

RunStatus = Literal[
    "running",
    "waiting_confirmation",
    "waiting_input",
    "waiting_tool_result",
    "waiting_approval",
    "completed",
    "cancelled",
    "failed",
    "expired",
    "handed_off",
]


class RunSummary(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    session_id: str
    end_user_ref: str
    source: str
    status: str
    outcome: str | None
    input_preview: str | None
    reply_preview: str | None
    tokens: int
    actions: int
    error: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None


class StepOut(BaseModel):
    seq: int
    type: str
    name: str
    status: str
    latency_ms: int
    tokens: int
    error: str | None
    input: Any = None
    output: Any = None
    created_at: datetime


class RunDetail(RunSummary):
    pending: dict[str, Any] | None
    steps: list[StepOut]


class RunList(BaseModel):
    items: list[RunSummary]
    total: int


@router.get("/runs", response_model=RunList)
def list_runs(
    agent_id: UUID | None = None,
    status_filter: RunStatus | None = Query(default=None, alias="status"),
    source: Literal["api", "playground", "test", "dashboard"] | None = None,
    end_user: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    items, total = svc.list_runs(
        db,
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        status=status_filter,
        source=source,
        end_user=end_user,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total}


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    run = svc.get_run(db, tenant_id=user.tenant_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


class UsageTotals(BaseModel):
    runs: int
    tokens: int
    action_calls: int
    failed: int


class UsageDay(BaseModel):
    date: str
    runs: int
    tokens: int
    action_calls: int


class UsageAgent(UsageTotals):
    agent_id: UUID
    name: str
    tokens_today: int = 0
    daily_budget: int | None = None


class UsageOut(BaseModel):
    days: int
    totals: UsageTotals
    series: list[UsageDay]
    by_agent: list[UsageAgent]


@router.get("/usage", response_model=UsageOut)
def usage(
    days: int = Query(default=30, ge=1, le=90),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    return svc.usage(db, tenant_id=user.tenant_id, days=days)


_replay_throttle = rate_limited("chat", per_user="chat_rate_per_user", per_tenant="chat_rate_per_tenant")


class ReplayIn(BaseModel):
    target: str = "draft"  # "draft" or a version number


@router.post("/runs/{run_id}/replay")
def replay_run(
    run_id: UUID,
    body: ReplayIn,
    user: User = Depends(admin_only),
    _limit: User = Depends(_replay_throttle),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Re-run this conversation against the draft or a version. Actions answer with what they
    returned the first time, so nothing is sent to your systems."""
    from app.services import scenarios

    try:
        return scenarios.replay_run(db, tenant_id=user.tenant_id, run_id=run_id, target=body.target)
    except scenarios.ScenarioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
