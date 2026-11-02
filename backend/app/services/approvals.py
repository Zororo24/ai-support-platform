"""Staff approvals: a write action that must be allowed by a person on the company's team.

The agent tells the customer their request has gone to the team and the run waits. When staff
decide, an approved action runs exactly as it was shown (arguments are stored, not re-derived),
the process continues, and the company's systems hear about the outcome through a webhook.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import executor
from app.agents.action_schema import ActionDef
from app.agents.config import parse_config
from app.agents.context import TurnContext
from app.core.config import get_settings
from app.db.models import Agent, AgentSession, AgentVersion, Approval, Conversation, Run, User
from app.services import tenant_llm, webhooks

logger = logging.getLogger(__name__)

DENIED_TEXT = "Your request was declined by our team."


class ApprovalError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request(
    ctx: TurnContext,
    run: Run,
    action: ActionDef,
    args: dict[str, Any],
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Create the approval and return the `pending` record stored on the run."""
    settings = get_settings()
    summary = executor.confirmation_summary(action, args, ctx.context)
    approval = Approval(
        tenant_id=ctx.tenant_id,
        agent_id=ctx.agent.id,
        run_id=run.id,
        action=action.name,
        args=args,
        summary=summary[:500],
        expires_at=_now() + timedelta(hours=settings.approvals_ttl_hours),
    )
    ctx.db.add(approval)
    ctx.db.commit()
    pending = {
        "kind": "approval",
        "id": str(approval.id),
        "action": action.name,
        "args": args,
        "args_preview": executor.redact(args),
        "summary": approval.summary,
        "node": node,
        "expires_at": approval.expires_at.isoformat(),
    }
    if ctx.conversation.source == "api":
        webhooks.emit(
            ctx.db,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent.id,
            event_type="approval.requested",
            data={
                "approval_id": str(approval.id),
                "run_id": str(run.id),
                "session_id": ctx.session.external_id,
                "end_user_id": ctx.session.end_user_ref,
                "action": action.name,
                "summary": approval.summary,
            },
        )
    return pending


def list_approvals(
    db: Session, *, tenant_id: UUID, status: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    conditions = [Approval.tenant_id == tenant_id]
    if status:
        conditions.append(Approval.status == status)
    total = db.scalar(select(func.count()).select_from(Approval).where(*conditions)) or 0
    rows = db.execute(
        select(Approval, Agent.name, AgentSession.end_user_ref, AgentSession.external_id)
        .join(Agent, Agent.id == Approval.agent_id)
        .join(Run, Run.id == Approval.run_id)
        .join(AgentSession, AgentSession.id == Run.session_id)
        .where(*conditions)
        .order_by(Approval.requested_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    emails = {}
    decider_ids = {a.decided_by for a, *_ in rows if a.decided_by}
    if decider_ids:
        emails = {
            r.id: r.email for r in db.execute(select(User.id, User.email).where(User.id.in_(decider_ids)))
        }
    items = [
        {
            "id": a.id,
            "agent_id": a.agent_id,
            "agent_name": agent_name,
            "run_id": a.run_id,
            "end_user_ref": end_user,
            "session_id": ext,
            "action": a.action,
            "summary": a.summary,
            "args": executor.redact(a.args),
            "status": a.status,
            "requested_at": a.requested_at,
            "expires_at": a.expires_at,
            "decided_by": emails.get(a.decided_by) if a.decided_by else None,
            "decided_at": a.decided_at,
            "note": a.note,
        }
        for a, agent_name, end_user, ext in rows
    ]
    return items, total


def _context_for(db: Session, run: Run) -> TurnContext:
    agent = db.get(Agent, run.agent_id)
    session = db.get(AgentSession, run.session_id)
    conversation = db.get(Conversation, run.conversation_id) if run.conversation_id else None
    if agent is None or session is None or conversation is None:
        raise ApprovalError("The conversation this request belongs to no longer exists.")
    raw = agent.draft_config
    if run.agent_version is not None:
        version = db.scalars(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version == run.agent_version
            )
        ).first()
        if version is not None:
            raw = version.config
    return TurnContext(
        db=db,
        agent=agent,
        config=parse_config(raw),
        version=run.agent_version,
        session=session,
        conversation=conversation,
        end_user_attrs=dict(session.end_user_attrs or {}),
        record_user=False,
    )


def decide(db: Session, *, user: User, approval_id: UUID, decision: str, note: str) -> dict[str, Any]:
    """Allow or refuse a pending approval. Returns the run's resulting response."""
    from app.agents import loop

    approval = db.execute(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == user.tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if approval is None:
        raise ApprovalError("Approval not found", 404)
    if approval.status != "pending":
        raise ApprovalError(f"This request was already {approval.status}.")
    if approval.expires_at < _now():
        approval.status = "expired"
        db.commit()
        raise ApprovalError("This request expired before it was decided.")
    run = db.execute(select(Run).where(Run.id == approval.run_id).with_for_update()).scalar_one()
    if run.status != "waiting_approval" or not run.pending:
        raise ApprovalError("The conversation has moved on; this request can no longer be run.")

    ctx = _context_for(db, run)
    usage = tenant_llm.bind(db, user.tenant_id)
    import time

    started = time.perf_counter()
    pending = dict(run.pending)
    approval.status = "approved" if decision == "approve" else "denied"
    approval.decided_by, approval.decided_at, approval.note = user.id, _now(), note[:300]
    db.commit()

    try:
        if decision == "deny":
            reply = DENIED_TEXT + (f" {note.strip()}" if note.strip() else "")
            result = loop.finish(
                ctx, run, user_message=run.input_preview or "", status="completed", outcome="denied",
                reply=reply, usage=usage, started=started,
            )  # fmt: skip
        else:
            run.status, run.pending = "running", None
            db.commit()
            events = list(loop.resume_approved(ctx, run, pending, usage, started))
            result = events[-1]["result"]
    except Exception as exc:
        db.rollback()
        loop.fail_run(ctx, run.id, f"{type(exc).__name__}: {exc}")
        raise

    if ctx.conversation.source == "api":
        webhooks.emit(
            db,
            tenant_id=user.tenant_id,
            agent_id=ctx.agent.id,
            event_type="approval.decided",
            data={
                "approval_id": str(approval.id),
                "approved": decision == "approve",
                "note": note[:300],
                "decided_by": user.email,
                "run_id": str(run.id),
                "session_id": ctx.session.external_id,
                "end_user_id": ctx.session.end_user_ref,
                "status": result["status"],
                "outcome": result["outcome"],
                "reply": result["reply"],
                "action_request": result.get("action_request"),
            },
        )
    return result


def expire_overdue(db: Session) -> int:
    """Pending approvals nobody decided in time are closed, and their runs end as expired."""
    overdue = db.scalars(
        select(Approval).where(Approval.status == "pending", Approval.expires_at < _now())
    ).all()
    for approval in overdue:
        approval.status = "expired"
        run = db.get(Run, approval.run_id)
        if run is not None and run.status == "waiting_approval":
            run.status, run.pending, run.state, run.ended_at = "expired", None, {}, _now()
    db.commit()
    return len(overdue)


__all__ = ["ApprovalError", "decide", "expire_overdue", "list_approvals", "request"]
