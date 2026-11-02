"""Scenario tests and version comparison for an agent (dashboard)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.config import parse_config
from app.api.deps import require_roles
from app.core.ratelimit import rate_limited
from app.db.models import Agent, AgentScenario, ScenarioRun, User, UserRole
from app.db.session import get_db
from app.services import agents as agents_service
from app.services import audit, config_diff
from app.services import scenarios as svc

router = APIRouter(prefix="/admin/agents", tags=["tests"])

admin_only = require_roles(UserRole.admin)
_run_throttle = rate_limited("chat", per_user="chat_rate_per_user", per_tenant="chat_rate_per_tenant")


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    definition: dict[str, Any]


class ScenarioPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    definition: dict[str, Any] | None = None


class LastResult(BaseModel):
    passed: bool
    target: str
    at: datetime


class ScenarioOut(BaseModel):
    id: UUID
    name: str
    enabled: bool
    definition: dict[str, Any]
    updated_at: datetime
    last_result: LastResult | None = None


class RunRequest(BaseModel):
    target: str = Field(default="draft", max_length=16)  # "draft" or a version number
    scenario_ids: list[UUID] | None = None


class RecordIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)


class ScenarioRunSummary(BaseModel):
    id: UUID
    target: str
    purpose: str
    passed: int
    failed: int
    created_at: datetime


class ScenarioRunOut(ScenarioRunSummary):
    results: list[dict[str, Any]]


def _require_agent(db: Session, user: User, agent_id: UUID) -> Agent:
    agent = agents_service.get_agent(db, tenant_id=user.tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


def _require_scenario(db: Session, agent: Agent, scenario_id: UUID) -> AgentScenario:
    row = svc.get_scenario(db, agent=agent, scenario_id=scenario_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test not found")
    return row


def _error(exc: svc.ScenarioError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _out(row: AgentScenario, last: ScenarioRun | None) -> ScenarioOut:
    result = None
    if last is not None:
        mine = next((r for r in last.results if r.get("scenario_id") == str(row.id)), None)
        if mine is not None:
            result = LastResult(passed=bool(mine["passed"]), target=last.target, at=last.created_at)
    return ScenarioOut(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        definition=row.definition,
        updated_at=row.updated_at,
        last_result=result,
    )


def _run_summary(run: ScenarioRun) -> ScenarioRunSummary:
    return ScenarioRunSummary(
        id=run.id,
        target=run.target,
        purpose=run.purpose,
        passed=run.passed,
        failed=run.failed,
        created_at=run.created_at,
    )


def _run_out(run: ScenarioRun) -> ScenarioRunOut:
    return ScenarioRunOut(**_run_summary(run).model_dump(), results=run.results)


@router.get("/{agent_id}/scenarios", response_model=list[ScenarioOut])
def list_scenarios(
    agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[ScenarioOut]:
    agent = _require_agent(db, user, agent_id)
    last = next(iter(svc.list_runs(db, agent=agent, limit=1)), None)
    return [_out(row, last) for row in svc.list_scenarios(db, agent=agent)]


@router.post("/{agent_id}/scenarios", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
def create_scenario(
    agent_id: UUID, body: ScenarioIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> ScenarioOut:
    agent = _require_agent(db, user, agent_id)
    try:
        row = svc.create_scenario(
            db, agent=agent, user=user, name=body.name, enabled=body.enabled, definition=body.definition
        )
    except svc.ScenarioError as exc:
        raise _error(exc)
    audit.record(
        db, actor=user, action="scenario.create", target_type="scenario", target_id=row.id,
        detail={"agent": str(agent.id), "name": row.name},
    )  # fmt: skip
    return _out(row, None)


@router.put("/{agent_id}/scenarios/{scenario_id}", response_model=ScenarioOut)
def update_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    body: ScenarioPatch,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ScenarioOut:
    agent = _require_agent(db, user, agent_id)
    row = _require_scenario(db, agent, scenario_id)
    try:
        svc.update_scenario(
            db, agent=agent, row=row, name=body.name, enabled=body.enabled, definition=body.definition
        )
    except svc.ScenarioError as exc:
        raise _error(exc)
    return _out(row, None)


@router.delete("/{agent_id}/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scenario(
    agent_id: UUID, scenario_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> None:
    agent = _require_agent(db, user, agent_id)
    row = _require_scenario(db, agent, scenario_id)
    name = row.name
    svc.delete_scenario(db, row=row)
    audit.record(
        db, actor=user, action="scenario.delete", target_type="scenario", target_id=scenario_id,
        detail={"agent": str(agent.id), "name": name},
    )  # fmt: skip


@router.post("/{agent_id}/scenarios/run", response_model=ScenarioRunOut)
def run_scenarios(
    agent_id: UUID,
    body: RunRequest,
    user: User = Depends(admin_only),
    _limit: User = Depends(_run_throttle),
    db: Session = Depends(get_db),
) -> ScenarioRunOut:
    """Play the agent's tests against the draft (default) or a published version."""
    agent = _require_agent(db, user, agent_id)
    try:
        run = svc.run_all(db, agent=agent, user=user, target=body.target, scenario_ids=body.scenario_ids)
    except svc.ScenarioError as exc:
        raise _error(exc)
    return _run_out(run)


@router.post("/{agent_id}/scenarios/record", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
def record_scenario(
    agent_id: UUID, body: RecordIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> ScenarioOut:
    """Save a playground conversation as a test, with expectations taken from what happened."""
    agent = _require_agent(db, user, agent_id)
    try:
        row = svc.record_from_conversation(
            db, agent=agent, user=user, session_id=body.session_id, name=body.name
        )
    except svc.ScenarioError as exc:
        raise _error(exc)
    audit.record(
        db, actor=user, action="scenario.record", target_type="scenario", target_id=row.id,
        detail={"agent": str(agent.id), "name": row.name},
    )  # fmt: skip
    return _out(row, None)


@router.get("/{agent_id}/scenario-runs", response_model=list[ScenarioRunSummary])
def list_scenario_runs(
    agent_id: UUID,
    limit: int = Query(default=20, ge=1, le=30),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> list[ScenarioRunSummary]:
    agent = _require_agent(db, user, agent_id)
    return [_run_summary(r) for r in svc.list_runs(db, agent=agent, limit=limit)]


@router.get("/{agent_id}/scenario-runs/{run_id}", response_model=ScenarioRunOut)
def get_scenario_run(
    agent_id: UUID, run_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> ScenarioRunOut:
    agent = _require_agent(db, user, agent_id)
    run = svc.get_run(db, agent=agent, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test run not found")
    return _run_out(run)


# ---- comparing configurations ------------------------------------------------------------------------------


def _config_for(db: Session, agent: Agent, which: str) -> dict[str, Any]:
    if which == "draft":
        raw = agent.draft_config
    else:
        number = agent.published_version if which == "live" else _as_int(which)
        found = agents_service.get_version(db, agent=agent, version=number) if number is not None else None
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No such version: {which}")
        raw = found.config
    try:
        return parse_config(raw).model_dump(mode="json")  # fills defaults so old versions compare fairly
    except Exception:
        return raw


def _as_int(value: str) -> int | None:
    try:
        return int(value.lstrip("v"))
    except ValueError:
        return None


@router.get("/{agent_id}/diff")
def diff(
    agent_id: UUID,
    base: str = Query(default="live", alias="from", max_length=10),
    to: str = Query(default="draft", max_length=10),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What differs between two configurations: `draft`, `live`, or a version number."""
    agent = _require_agent(db, user, agent_id)
    return {
        "from": base,
        "to": to,
        **config_diff.diff_configs(_config_for(db, agent, base), _config_for(db, agent, to)),
    }
