"""Dashboard API (staff JWT): manage agents, versions, API keys, and test in the playground."""

import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import workflow_templates as workflow_templates_module
from app.agents.config import parse_config
from app.agents.runtime import ConfirmationNotFound
from app.api.deps import require_roles
from app.api.schemas.agents import (
    ActionTestIn,
    ActionTestOut,
    AgentCreate,
    AgentDetail,
    AgentPatch,
    AgentSummary,
    KeyCreate,
    KeyCreated,
    KeyIpsIn,
    KeyOut,
    PlaygroundConfirmIn,
    PlaygroundIn,
    PublishIn,
    VersionDetail,
    VersionOut,
)
from app.api.schemas.v1 import AgentMessageOut
from app.core.llm import LLMUnavailable
from app.core.ratelimit import rate_limited
from app.db.models import Agent, ApiKey, User, UserRole
from app.db.session import get_db
from app.services import action_test, agent_turn, audit
from app.services import agents as svc
from app.services import collections as collections_service
from app.services import scenarios as scenarios_service
from app.services import sessions as sessions_service

router = APIRouter(prefix="/admin/agents", tags=["agents"])

admin_only = require_roles(UserRole.admin)
_playground_throttle = rate_limited("chat", per_user="chat_rate_per_user", per_tenant="chat_rate_per_tenant")


def _require(db: Session, user: User, agent_id: UUID) -> Agent:
    agent = svc.get_agent(db, tenant_id=user.tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


def _unpublished_changes(db: Session, agent: Agent) -> bool:
    live = svc.live_config(db, agent=agent)
    if live is None:
        return True
    try:
        draft = parse_config(agent.draft_config).model_dump(mode="json")
    except Exception:
        return True
    return draft != live[0].model_dump(mode="json")


def _summary(db: Session, agent: Agent) -> AgentSummary:
    return AgentSummary(
        id=agent.id,
        name=agent.name,
        slug=agent.slug,
        description=agent.description,
        published_version=agent.published_version,
        has_unpublished_changes=_unpublished_changes(db, agent),
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _detail(db: Session, agent: Agent) -> AgentDetail:
    return AgentDetail(**_summary(db, agent).model_dump(), draft_config=agent.draft_config)


def _key_out(key: ApiKey) -> KeyOut:
    return KeyOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        allowed_ips=list(key.allowed_ips or []),
    )


def _agent_error(exc: svc.AgentError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("", response_model=list[AgentSummary])
def list_agents(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[AgentSummary]:
    return [_summary(db, a) for a in svc.list_agents(db, tenant_id=user.tenant_id)]


@router.post("", response_model=AgentDetail, status_code=status.HTTP_201_CREATED)
def create_agent(
    body: AgentCreate, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> AgentDetail:
    config: dict[str, Any] = {}
    if body.template == "support":
        default = collections_service.default_collection(db, tenant_id=user.tenant_id)
        config = {
            "instructions": "Be concise, friendly and accurate.",
            "knowledge": {"collection_ids": [str(default.id)]},
        }
    try:
        agent = svc.create_agent(
            db,
            user=user,
            tenant_id=user.tenant_id,
            name=body.name,
            description=body.description,
            config=config,
        )
    except svc.AgentError as exc:
        raise _agent_error(exc)
    audit.record(
        db, actor=user, action="agent.create", target_type="agent", target_id=agent.id,
        detail={"name": agent.name, "template": body.template},
    )  # fmt: skip
    return _detail(db, agent)


@router.get("/{agent_id}", response_model=AgentDetail)
def get_agent(agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> AgentDetail:
    return _detail(db, _require(db, user, agent_id))


@router.patch("/{agent_id}", response_model=AgentDetail)
def patch_agent(
    agent_id: UUID, body: AgentPatch, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> AgentDetail:
    agent = svc.update_agent(
        db, agent=_require(db, user, agent_id), name=body.name, description=body.description
    )
    return _detail(db, agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    agent = _require(db, user, agent_id)
    name = agent.name
    svc.delete_agent(db, agent=agent)
    audit.record(
        db, actor=user, action="agent.delete", target_type="agent", target_id=agent_id, detail={"name": name}
    )


@router.put("/{agent_id}/draft", response_model=AgentDetail)
def put_draft(
    agent_id: UUID,
    config: dict[str, Any],
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> AgentDetail:
    """Replace the draft configuration. Fully validated; nothing goes live until published."""
    agent = _require(db, user, agent_id)
    try:
        svc.save_draft(db, agent=agent, raw=config)
    except svc.AgentError as exc:
        raise _agent_error(exc)
    return _detail(db, agent)


# ---------- versions ----------


def _version_out(db: Session, agent: Agent, version, emails: dict[UUID, str]) -> VersionOut:
    return VersionOut(
        version=version.version,
        note=version.note,
        created_at=version.created_at,
        created_by_email=emails.get(version.created_by) if version.created_by else None,
        is_live=agent.published_version == version.version,
        tests=version.test_summary,
    )


def _emails(db: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return {r.id: r.email for r in db.execute(select(User.id, User.email).where(User.id.in_(ids)))}


@router.get("/{agent_id}/versions", response_model=list[VersionOut])
def list_versions(
    agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[VersionOut]:
    agent = _require(db, user, agent_id)
    versions = svc.list_versions(db, agent=agent)
    emails = _emails(db, {v.created_by for v in versions if v.created_by})
    return [_version_out(db, agent, v, emails) for v in versions]


@router.get("/{agent_id}/versions/{version}", response_model=VersionDetail)
def get_version(
    agent_id: UUID, version: int, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> VersionDetail:
    agent = _require(db, user, agent_id)
    found = svc.get_version(db, agent=agent, version=version)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    base = _version_out(db, agent, found, _emails(db, {found.created_by} if found.created_by else set()))
    return VersionDetail(**base.model_dump(), config=found.config)


def _run_gate(db: Session, agent: Agent, user: User, config, body: PublishIn) -> dict[str, Any]:
    """Play the agent's enabled tests against the draft. Failures block publishing."""
    enabled = [s for s in scenarios_service.list_scenarios(db, agent=agent) if s.enabled]
    summary: dict[str, Any] = {"passed": 0, "failed": 0, "skipped": False, "override_reason": None}
    if not enabled:
        return summary
    if body.skip_tests:
        reason = body.skip_reason.strip()
        if not reason:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Say why you are publishing without running the tests.",
            )
        return {**summary, "skipped": True, "override_reason": reason}
    snapshot = dict(agent.draft_config)
    run = scenarios_service.run_all(
        db, agent=agent, user=user, target="draft", purpose="publish_gate", config=config
    )
    db.refresh(agent)
    if dict(agent.draft_config) != snapshot:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The draft changed while the tests were running. Try publishing again.",
        )
    if run.failed:
        names = ", ".join(r["name"] for r in run.results if not r["passed"])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{run.failed} of {run.passed + run.failed} tests failed ({names}). "
                "Fix them, or publish anyway and say why."
            ),
        )
    return {**summary, "passed": run.passed}


@router.post("/{agent_id}/publish", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
def publish(
    agent_id: UUID, body: PublishIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> VersionOut:
    agent = _require(db, user, agent_id)
    try:
        config = svc.validate_config(db, tenant_id=agent.tenant_id, raw=agent.draft_config)
    except svc.AgentError as exc:
        raise _agent_error(exc)
    summary = _run_gate(db, agent, user, config, body)
    try:
        version = svc.publish(db, agent=agent, user=user, note=body.note, test_summary=summary)
    except svc.AgentError as exc:
        raise _agent_error(exc)
    audit.record(
        db, actor=user, action="agent.publish", target_type="agent", target_id=agent.id,
        detail={"version": version.version, "note": version.note, "tests": summary},
    )  # fmt: skip
    return _version_out(db, agent, version, {user.id: user.email})


@router.post("/{agent_id}/versions/{version}/make-live", response_model=VersionOut)
def make_live(
    agent_id: UUID, version: int, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> VersionOut:
    """Roll back (or forward) to an existing version without creating a new one."""
    agent = _require(db, user, agent_id)
    try:
        target = svc.make_live(db, agent=agent, version=version)
    except svc.AgentError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    audit.record(
        db, actor=user, action="agent.make_live", target_type="agent", target_id=agent.id,
        detail={"version": target.version},
    )  # fmt: skip
    return _version_out(db, agent, target, _emails(db, {target.created_by} if target.created_by else set()))


# ---------- API keys ----------


@router.get("/{agent_id}/keys", response_model=list[KeyOut])
def list_keys(
    agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[KeyOut]:
    return [_key_out(k) for k in svc.list_keys(db, agent=_require(db, user, agent_id))]


@router.post("/{agent_id}/keys", response_model=KeyCreated, status_code=status.HTTP_201_CREATED)
def create_key(
    agent_id: UUID, body: KeyCreate, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> KeyCreated:
    agent = _require(db, user, agent_id)
    try:
        key, full = svc.create_key(db, agent=agent, user=user, name=body.name, allowed_ips=body.allowed_ips)
    except svc.AgentError as exc:
        raise _agent_error(exc)
    audit.record(
        db, actor=user, action="api_key.create", target_type="api_key", target_id=key.id,
        detail={"agent": agent.name, "name": key.name, "prefix": key.prefix},
    )  # fmt: skip
    return KeyCreated(**_key_out(key).model_dump(), key=full)


@router.put("/{agent_id}/keys/{key_id}/allowed-ips", response_model=KeyOut)
def set_key_ips(
    agent_id: UUID,
    key_id: UUID,
    body: KeyIpsIn,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> KeyOut:
    """Limit a key to calling from these addresses or ranges (empty list = anywhere)."""
    agent = _require(db, user, agent_id)
    try:
        key = svc.set_key_ips(db, agent=agent, key_id=key_id, allowed_ips=body.allowed_ips)
    except svc.AgentError as exc:
        raise _agent_error(exc)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    audit.record(
        db, actor=user, action="api_key.set_allowed_ips", target_type="api_key", target_id=key.id,
        detail={"agent": agent.name, "prefix": key.prefix, "allowed_ips": key.allowed_ips},
    )  # fmt: skip
    return _key_out(key)


@router.delete("/{agent_id}/keys/{key_id}", response_model=KeyOut)
def revoke_key(
    agent_id: UUID, key_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> KeyOut:
    agent = _require(db, user, agent_id)
    key = svc.revoke_key(db, agent=agent, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    audit.record(
        db, actor=user, action="api_key.revoke", target_type="api_key", target_id=key.id,
        detail={"agent": agent.name, "prefix": key.prefix},
    )  # fmt: skip
    return _key_out(key)


# ---------- workflow templates ----------


class WorkflowTemplateOut(BaseModel):
    id: str
    name: str
    description: str
    ready: bool
    missing: list[str]
    workflow: dict[str, Any]


@router.get("/{agent_id}/workflow-templates", response_model=list[WorkflowTemplateOut])
def workflow_templates(
    agent_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> list[dict]:
    """Ready-made workflows, each marked with whether this agent has the actions it relies on."""
    agent = _require(db, user, agent_id)
    try:
        actions = parse_config(agent.draft_config).actions
    except Exception:
        actions = []
    out = []
    for template in workflow_templates_module.TEMPLATES:
        missing = workflow_templates_module.readiness(template, actions)
        out.append(
            {
                "id": template["id"],
                "name": template["name"],
                "description": template["description"],
                "ready": not missing,
                "missing": missing,
                "workflow": template["workflow"],
            }
        )
    return out


# ---------- playground & action tests ----------


def _playground_config(db: Session, agent: Agent, which: str):
    if which == "published":
        live = svc.live_config(db, agent=agent)
        if live is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not published yet")
        return live
    try:
        return parse_config(agent.draft_config), None
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Draft is invalid")


def _playground_session(db: Session, agent: Agent, user: User, session_ext: str, attrs: dict):
    try:
        return agent_turn.open_conversation(
            db,
            agent=agent,
            external_session_id=session_ext,
            end_user_ref=f"staff:{user.id}",
            end_user_attrs=attrs,
            source="playground",
        )
    except sessions_service.SessionOwnerMismatch:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Session belongs to someone else")


_UNAVAILABLE = "The assistant is temporarily unavailable. Please try again in a moment."


@router.post("/{agent_id}/playground/messages", response_model=AgentMessageOut)
def playground_message(
    agent_id: UUID,
    body: PlaygroundIn,
    user: User = Depends(admin_only),
    _limit: User = Depends(_playground_throttle),
    db: Session = Depends(get_db),
) -> AgentMessageOut:
    """Chat with the agent as staff. Runs the unpublished draft by default.

    With `mock_actions`, actions answer from their canned `mock` instead of calling the
    company's system, so processes can be tried safely.
    """
    agent = _require(db, user, agent_id)
    config, version = _playground_config(db, agent, body.version)
    if body.workflow is not None and body.workflow not in {w.name for w in config.workflows if w.enabled}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No such process")
    session_ext = body.session_id or "pg_" + secrets.token_hex(8)
    session, conversation = _playground_session(db, agent, user, session_ext, body.end_user_attributes)
    try:
        events = list(
            agent_turn.execute(
                db,
                agent_id=agent.id,
                session_id=session.id,
                conversation_id=conversation.id,
                config=config,
                version=version,
                message=body.message,
                use_mocks=body.mock_actions,
                workflow=body.workflow,
            )
        )
    except LLMUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE)
    return AgentMessageOut(session_id=session_ext, **events[-1]["result"])


@router.post("/{agent_id}/playground/confirmations", response_model=AgentMessageOut)
def playground_confirmation(
    agent_id: UUID,
    body: PlaygroundConfirmIn,
    user: User = Depends(admin_only),
    _limit: User = Depends(_playground_throttle),
    db: Session = Depends(get_db),
) -> AgentMessageOut:
    agent = _require(db, user, agent_id)
    config, version = _playground_config(db, agent, body.version)
    session = sessions_service.get_session(db, agent=agent, external_id=body.session_id)
    if session is None or session.end_user_ref != f"staff:{user.id}":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    conversation = sessions_service.get_or_create_conversation(db, session=session, source="playground")
    try:
        events = list(
            agent_turn.confirm(
                db,
                agent_id=agent.id,
                session_id=session.id,
                conversation_id=conversation.id,
                config=config,
                version=version,
                confirmation_id=body.confirmation_id,
                decision=body.decision,
                use_mocks=body.mock_actions,
            )
        )
    except ConfirmationNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Nothing is waiting for that confirmation"
        )
    except LLMUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE)
    return AgentMessageOut(session_id=body.session_id, **events[-1]["result"])


@router.post("/{agent_id}/actions/{action_name}/test", response_model=ActionTestOut)
def test_action(
    agent_id: UUID,
    action_name: str,
    body: ActionTestIn,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    """Try one action from the draft with sample arguments and a sample end-user context.

    Shows the exact request that would be sent (credentials masked) and what the model would see
    of the answer. Actions that change data in the company's system need `send_for_real`
    (or `mock`) so a test never writes by accident.
    """
    agent = _require(db, user, agent_id)
    try:
        return action_test.run(
            db,
            agent=agent,
            action_name=action_name,
            arguments=body.arguments,
            context=body.context,
            use_mock=body.mock,
            send_for_real=body.send_for_real,
        )
    except action_test.ActionTestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
