"""Periodic cleanup, run by the worker every few minutes.

It repairs the things a restart or an unattended customer leaves behind: runs that waited too long,
documents stuck mid-indexing, and old job/delivery rows.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    AgentSession,
    Conversation,
    Document,
    DocumentStatus,
    Job,
    Run,
    WebhookDelivery,
)
from app.db.session import get_session_factory
from app.services import jobs

logger = logging.getLogger(__name__)

SWEEP = "housekeeping.sweep"
INTERVAL_SECONDS = 300
STUCK_DOCUMENT_AFTER = timedelta(minutes=10)
KEEP_JOBS_FOR = timedelta(days=7)
KEEP_DELIVERIES_FOR = timedelta(days=30)
KEEP_TEST_CONVERSATIONS_FOR = timedelta(days=3)  # scenario-test and replay sessions
_last_check = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def expire_runs(db: Session) -> int:
    result = db.execute(
        update(Run)
        .where(
            Run.status.in_(("waiting_confirmation", "waiting_input", "waiting_tool_result")),
            Run.expires_at < _now(),
        )
        .values(status="expired", pending=None, state={}, ended_at=_now())
    )
    db.commit()
    return result.rowcount or 0


def requeue_stuck_documents(db: Session) -> int:
    """A document left in pending/processing (e.g. the API restarted mid-index) gets indexed again."""
    stuck = db.scalars(
        select(Document).where(
            Document.status.in_((DocumentStatus.pending, DocumentStatus.processing)),
            Document.updated_at < _now() - STUCK_DOCUMENT_AFTER,
        )
    ).all()
    for document in stuck:
        document.updated_at = _now()  # so it is not requeued again on the next sweep
        job = jobs.enqueue(
            db, type="document.index", payload={"document_id": str(document.id)}, tenant_id=document.tenant_id
        )
        jobs.kick(job.id)
    db.commit()
    return len(stuck)


def prune(db: Session) -> int:
    old_jobs = db.execute(
        delete(Job).where(Job.status.in_(("done", "failed")), Job.created_at < _now() - KEEP_JOBS_FOR)
    ).rowcount
    old_deliveries = db.execute(
        delete(WebhookDelivery).where(WebhookDelivery.created_at < _now() - KEEP_DELIVERIES_FOR)
    ).rowcount
    old_tests = db.execute(
        delete(AgentSession).where(
            AgentSession.id.in_(
                select(Conversation.session_id).where(
                    Conversation.source == "test",
                    Conversation.created_at < _now() - KEEP_TEST_CONVERSATIONS_FOR,
                )
            )
        )
    ).rowcount
    db.commit()
    return (old_jobs or 0) + (old_deliveries or 0) + (old_tests or 0)


def apply_retention(db: Session) -> int:
    """Delete conversations nobody has touched for as long as their agent's privacy setting allows.

    Each live agent's `retention_days` applies to its own sessions (with their messages, runs and
    trace steps). Agents that were never published have no real customers to forget.
    """
    from app.services import agents as agents_service

    deleted = 0
    for agent in db.scalars(select(Agent).where(Agent.published_version.is_not(None))):
        live = agents_service.live_config(db, agent=agent)
        if live is None:
            continue
        cutoff = _now() - timedelta(days=live[0].privacy.retention_days)
        deleted += (
            db.execute(
                delete(AgentSession).where(
                    AgentSession.agent_id == agent.id,
                    AgentSession.tenant_id == agent.tenant_id,
                    AgentSession.last_active_at < cutoff,
                )
            ).rowcount
            or 0
        )
    db.commit()
    return deleted


def run_sweep(db: Session) -> dict[str, int]:
    from app.services import approvals

    return {
        "runs_expired": expire_runs(db),
        "approvals_expired": approvals.expire_overdue(db),
        "documents_requeued": requeue_stuck_documents(db),
        "rows_pruned": prune(db),
        "sessions_expired": apply_retention(db),
    }


@jobs.handler(SWEEP)
def sweep(db: Session, payload: dict[str, Any]) -> None:
    summary = run_sweep(db)
    if any(summary.values()):
        logger.info("housekeeping: %s", summary)
    jobs.enqueue(db, type=SWEEP, payload={}, delay_seconds=INTERVAL_SECONDS, max_attempts=3)


def ensure_scheduled() -> None:
    """Make sure a sweep is queued. Cheap; the worker calls it about once a minute."""
    global _last_check
    if time.monotonic() - _last_check < 60:
        return
    _last_check = time.monotonic()
    with get_session_factory()() as db:
        if jobs.pending_count(db, SWEEP) == 0:
            jobs.enqueue(db, type=SWEEP, payload={}, max_attempts=3)
