"""Daily token budgets per agent (and optionally per workspace), with alerts.

A budget is a soft ceiling on the model tokens an agent may use per UTC day. Once the day's total
reaches it, new customer messages are refused with `429 budget_exceeded` until midnight UTC; a turn
already in progress finishes. Alerts go out as webhook events at 80% and again at 100%, once per
agent per day.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.config import AgentConfig
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.db.models import Agent, Run
from app.services import webhooks

logger = logging.getLogger(__name__)

WARN_AT = 0.8


class BudgetExceeded(Exception):
    def __init__(self, scope: str, retry_after: int):
        super().__init__(scope)
        self.scope = scope  # "agent" | "workspace"
        self.retry_after = retry_after


def _start_of_day() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_midnight() -> int:
    return max(1, int((_start_of_day() + timedelta(days=1) - datetime.now(timezone.utc)).total_seconds()))


def tokens_today(db: Session, *, tenant_id: UUID, agent_id: UUID | None = None) -> int:
    stmt = select(func.coalesce(func.sum(Run.tokens), 0)).where(
        Run.tenant_id == tenant_id, Run.started_at >= _start_of_day()
    )
    if agent_id is not None:
        stmt = stmt.where(Run.agent_id == agent_id)
    return int(db.scalar(stmt) or 0)


def check(db: Session, *, agent: Agent, config: AgentConfig) -> None:
    """Raise BudgetExceeded when today's tokens are already at the agent's or workspace's ceiling."""
    workspace = get_settings().tenant_daily_token_budget
    if workspace and tokens_today(db, tenant_id=agent.tenant_id) >= workspace:
        raise BudgetExceeded("workspace", seconds_until_midnight())
    budget = config.limits.daily_token_budget
    if budget and tokens_today(db, tenant_id=agent.tenant_id, agent_id=agent.id) >= budget:
        raise BudgetExceeded("agent", seconds_until_midnight())


def _first_today(key: str) -> bool:
    """True only the first time `key` is seen today. Without Redis nobody is alerted (no duplicates)."""
    try:
        return bool(get_redis().set(key, "1", nx=True, ex=26 * 3600))
    except Exception:
        return False


class Subject(NamedTuple):
    """Just the facts about an agent that alerts need, safe to use after the request's session closed."""

    tenant_id: UUID
    agent_id: UUID
    name: str


def after_turn(db: Session, *, who: Subject, config: AgentConfig) -> None:
    """Send the 80% and 100% alerts. Never allowed to break the response."""
    budget = config.limits.daily_token_budget
    if not budget:
        return
    try:
        used = tokens_today(db, tenant_id=who.tenant_id, agent_id=who.agent_id)
        today = _start_of_day().date().isoformat()
        for event, reached in (
            ("budget.exceeded", used >= budget),
            ("budget.warning", used >= WARN_AT * budget),
        ):
            if reached and _first_today(f"budget-alert:{who.agent_id}:{today}:{event}"):
                webhooks.emit(
                    db,
                    tenant_id=who.tenant_id,
                    agent_id=who.agent_id,
                    event_type=event,
                    data={"agent": who.name, "used_tokens": used, "daily_budget": budget, "date": today},
                )
    except Exception:
        logger.exception("could not send budget alerts for agent %s", who.agent_id)
        db.rollback()


def usage_for_agents(db: Session, *, tenant_id: UUID, agents: list[Agent]) -> dict[UUID, dict[str, Any]]:
    """Today's tokens and the configured ceiling for each agent (null = unlimited)."""
    from app.services import agents as agents_service

    out: dict[UUID, dict[str, Any]] = {}
    for agent in agents:
        live = agents_service.live_config(db, agent=agent)
        budget = live[0].limits.daily_token_budget if live else None
        out[agent.id] = {
            "tokens_today": tokens_today(db, tenant_id=tenant_id, agent_id=agent.id),
            "daily_budget": budget,
        }
    return out
