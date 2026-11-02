"""Read side of agent runs: the list, one run's full trace, and usage totals."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Agent, AgentSession, Conversation, Run, RunStep


def list_runs(
    db: Session,
    *,
    tenant_id: UUID,
    agent_id: UUID | None = None,
    status: str | None = None,
    source: str | None = None,
    end_user: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    conditions = [Run.tenant_id == tenant_id]
    if agent_id is not None:
        conditions.append(Run.agent_id == agent_id)
    if status:
        conditions.append(Run.status == status)
    if source:
        conditions.append(Conversation.source == source)
    if end_user:
        conditions.append(AgentSession.end_user_ref == end_user)

    base = (
        select(Run, Agent.name, AgentSession.external_id, AgentSession.end_user_ref, Conversation.source)
        .join(Agent, Agent.id == Run.agent_id)
        .join(AgentSession, AgentSession.id == Run.session_id)
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .where(*conditions)
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(Run)
            .join(AgentSession, AgentSession.id == Run.session_id)
            .outerjoin(Conversation, Conversation.id == Run.conversation_id)
            .where(*conditions)
        )
        or 0
    )
    rows = db.execute(base.order_by(Run.started_at.desc()).limit(limit).offset(offset)).all()
    action_counts = _action_counts(db, [r[0].id for r in rows])
    items = [
        _summary(run, agent_name, ext, end_user_ref, src, action_counts.get(run.id, 0))
        for run, agent_name, ext, end_user_ref, src in rows
    ]
    return items, total


def _action_counts(db: Session, run_ids: list[UUID]) -> dict[UUID, int]:
    if not run_ids:
        return {}
    rows = db.execute(
        select(RunStep.run_id, func.count())
        .where(RunStep.run_id.in_(run_ids), RunStep.type == "action")
        .group_by(RunStep.run_id)
    ).all()
    return dict(rows)


def _summary(
    run: Run, agent_name: str, ext: str, end_user_ref: str, source: str | None, actions: int
) -> dict[str, Any]:
    duration_ms = None
    if run.ended_at is not None:
        duration_ms = round((run.ended_at - run.started_at).total_seconds() * 1000)
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "agent_name": agent_name,
        "agent_version": run.agent_version,
        "session_id": ext,
        "end_user_ref": end_user_ref,
        "source": source or "api",
        "status": run.status,
        "outcome": run.outcome,
        "input_preview": run.input_preview,
        "reply_preview": run.reply_preview,
        "tokens": run.tokens,
        "actions": actions,
        "error": run.error,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "duration_ms": duration_ms,
    }


def get_run(db: Session, *, tenant_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    row = db.execute(
        select(Run, Agent.name, AgentSession.external_id, AgentSession.end_user_ref, Conversation.source)
        .join(Agent, Agent.id == Run.agent_id)
        .join(AgentSession, AgentSession.id == Run.session_id)
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .where(Run.id == run_id, Run.tenant_id == tenant_id)
    ).first()
    if row is None:
        return None
    run, agent_name, ext, end_user_ref, source = row
    steps = db.scalars(
        select(RunStep).where(RunStep.run_id == run.id, RunStep.tenant_id == tenant_id).order_by(RunStep.seq)
    )
    action_total = db.scalar(
        select(func.count()).select_from(RunStep).where(RunStep.run_id == run.id, RunStep.type == "action")
    )
    detail = _summary(run, agent_name, ext, end_user_ref, source, action_total or 0)
    detail["pending"] = (
        {k: (run.pending or {}).get(k) for k in ("id", "action", "summary", "args_preview", "expires_at")}
        if run.pending
        else None
    )
    detail["steps"] = [
        {
            "seq": s.seq,
            "type": s.type,
            "name": s.name,
            "status": s.status,
            "latency_ms": s.latency_ms,
            "tokens": s.tokens,
            "error": s.error,
            "input": s.input,
            "output": s.output,
            "created_at": s.created_at,
        }
        for s in steps
    ]
    return detail


def usage(db: Session, *, tenant_id: UUID, days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    day = func.date(func.timezone("UTC", Run.started_at))

    run_rows = db.execute(
        select(
            day.label("day"),
            Run.agent_id,
            func.count().label("runs"),
            func.coalesce(func.sum(Run.tokens), 0).label("tokens"),
            func.count().filter(Run.status == "failed").label("failed"),
        )
        .where(Run.tenant_id == tenant_id, Run.started_at >= since)
        .group_by(day, Run.agent_id)
    ).all()
    action_rows = db.execute(
        select(day.label("day"), Run.agent_id, func.count().label("calls"))
        .select_from(RunStep)
        .join(Run, Run.id == RunStep.run_id)
        .where(RunStep.tenant_id == tenant_id, RunStep.type == "action", Run.started_at >= since)
        .group_by(day, Run.agent_id)
    ).all()
    names = {a.id: a.name for a in db.scalars(select(Agent).where(Agent.tenant_id == tenant_id))}

    per_day: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "tokens": 0, "action_calls": 0})
    per_agent: dict[UUID, dict[str, int]] = defaultdict(
        lambda: {"runs": 0, "tokens": 0, "action_calls": 0, "failed": 0}
    )
    for d, agent_id, runs, tokens, failed in run_rows:
        key = d.isoformat()
        per_day[key]["runs"] += runs
        per_day[key]["tokens"] += int(tokens)
        per_agent[agent_id]["runs"] += runs
        per_agent[agent_id]["tokens"] += int(tokens)
        per_agent[agent_id]["failed"] += failed
    for d, agent_id, calls in action_rows:
        per_day[d.isoformat()]["action_calls"] += calls
        per_agent[agent_id]["action_calls"] += calls

    series = []
    for offset in range(days - 1, -1, -1):
        key = (now - timedelta(days=offset)).date().isoformat()
        series.append({"date": key, **per_day.get(key, {"runs": 0, "tokens": 0, "action_calls": 0})})

    totals = {
        "runs": sum(v["runs"] for v in per_agent.values()),
        "tokens": sum(v["tokens"] for v in per_agent.values()),
        "action_calls": sum(v["action_calls"] for v in per_agent.values()),
        "failed": sum(v["failed"] for v in per_agent.values()),
    }
    by_agent = sorted(
        (
            {"agent_id": agent_id, "name": names.get(agent_id, "(deleted agent)"), **stats}
            for agent_id, stats in per_agent.items()
        ),
        key=lambda row: row["runs"],
        reverse=True,
    )
    from app.services import budgets

    agents = list(db.scalars(select(Agent).where(Agent.tenant_id == tenant_id)))
    today = budgets.usage_for_agents(db, tenant_id=tenant_id, agents=agents)
    for row in by_agent:
        row.update(today.get(row["agent_id"], {"tokens_today": 0, "daily_budget": None}))
    return {"days": days, "totals": totals, "series": series, "by_agent": by_agent}
