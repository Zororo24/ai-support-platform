"""A small durable job queue on Postgres (no extra infrastructure).

Jobs are rows. A worker claims due rows with `FOR UPDATE SKIP LOCKED`, so several workers can run
safely, a crash just leaves a row to be picked up again, and a retry is a row whose `run_at` is
in the future. Used for webhook delivery, document indexing and housekeeping.

Modes (setting JOBS_MODE):
  embedded  the API process runs a worker thread (development default; zero setup)
  external  a separate `python -m app.worker` process does the work (production)
  sync      `kick()` runs a job immediately in the caller's thread (tests)
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# Seconds to wait before retry 1, 2, 3...: about a day of trying in total.
BACKOFF_SECONDS = [10, 60, 300, 1800, 7200, 21600, 43200]
STALE_AFTER = timedelta(minutes=5)  # a "running" job this old is assumed to belong to a dead worker

Handler = Callable[[Session, dict[str, Any]], None]
HANDLERS: dict[str, Handler] = {}


class RetryLater(Exception):
    """Raised by a handler to ask for another attempt later (counts as an attempt)."""

    def __init__(self, message: str, delay_seconds: int | None = None):
        super().__init__(message)
        self.delay_seconds = delay_seconds


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    db: Session,
    *,
    type: str,
    payload: dict[str, Any],
    tenant_id: UUID | None = None,
    delay_seconds: int = 0,
    max_attempts: int = len(BACKOFF_SECONDS) + 1,
) -> Job:
    job = Job(
        type=type,
        payload=payload,
        tenant_id=tenant_id,
        run_at=_now() + timedelta(seconds=delay_seconds),
        max_attempts=max_attempts,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def kick(job_id: UUID) -> None:
    """In sync mode run the job now; otherwise a worker will pick it up."""
    if get_settings().jobs_mode == "sync":
        with get_session_factory()() as db:
            run_job(db, job_id)


def claim(db: Session, limit: int = 10) -> list[UUID]:
    rows = db.scalars(
        select(Job)
        .where(Job.status == "queued", Job.run_at <= _now())
        .order_by(Job.run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status, job.locked_at, job.attempts = "running", _now(), job.attempts + 1
    db.commit()
    return [j.id for j in rows]


def requeue_stale(db: Session) -> int:
    result = db.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < _now() - STALE_AFTER)
        .values(status="queued", locked_at=None)
    )
    db.commit()
    return result.rowcount or 0


def run_job(db: Session, job_id: UUID) -> None:
    """Execute one claimed (or sync-mode) job and record how it went."""
    job = db.get(Job, job_id)
    if job is None or job.status == "done":
        return
    if job.status == "queued":  # sync mode runs jobs straight from the queued state
        job.status, job.locked_at, job.attempts = "running", _now(), job.attempts + 1
        db.commit()
    fn = HANDLERS.get(job.type)
    started = time.perf_counter()
    try:
        if fn is None:
            raise RuntimeError(f"no handler registered for job type '{job.type}'")
        fn(db, dict(job.payload))
    except RetryLater as exc:
        db.rollback()
        _reschedule(db, job_id, str(exc), exc.delay_seconds)
    except Exception as exc:
        db.rollback()
        logger.exception("job %s (%s) failed", job_id, job.type)
        _reschedule(db, job_id, f"{type(exc).__name__}: {exc}", None)
    else:
        job = db.get(Job, job_id)
        job.status, job.last_error, job.locked_at = "done", None, None
        db.commit()
        logger.info("job %s (%s) done in %d ms", job_id, job.type, (time.perf_counter() - started) * 1000)


def _reschedule(db: Session, job_id: UUID, error: str, delay: int | None) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.last_error, job.locked_at = error[:500], None
    if job.attempts >= job.max_attempts:
        job.status = "failed"
    else:
        wait = (
            delay if delay is not None else BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        )
        job.status, job.run_at = "queued", _now() + timedelta(seconds=wait)
    db.commit()


def pending_count(db: Session, job_type: str) -> int:
    from sqlalchemy import func

    return (
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.type == job_type, Job.status.in_(("queued", "running")))
        )
        or 0
    )
