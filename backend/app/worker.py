"""Background worker: runs queued jobs (webhook deliveries, document indexing, housekeeping).

Run it as its own process in production:    python -m app.worker
In development the API starts the same loop in a thread (JOBS_MODE=embedded, the default).
"""

import logging
import signal
import threading

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_session_factory
from app.services import jobs

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
BATCH = 10


def _load_handlers() -> None:
    """Importing these modules registers their job handlers."""
    from app.services import documents as _documents  # noqa: F401
    from app.services import housekeeping, webhooks  # noqa: F401


def tick() -> int:
    """Claim and run one batch of due jobs. Returns how many ran."""
    _load_handlers()
    factory = get_session_factory()
    with factory() as db:
        jobs.requeue_stale(db)
        ids = jobs.claim(db, BATCH)
    for job_id in ids:
        with factory() as db:
            jobs.run_job(db, job_id)
    return len(ids)


def run_forever(stop: threading.Event, poll_seconds: float = POLL_SECONDS) -> None:
    logger.info("worker started")
    _load_handlers()
    while not stop.is_set():
        try:
            from app.services import housekeeping

            housekeeping.ensure_scheduled()
            if tick() == 0:
                stop.wait(poll_seconds)
        except Exception:
            logger.exception("worker loop error; continuing")
            stop.wait(poll_seconds * 5)
    logger.info("worker stopped")


def start_embedded() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run_forever, args=(stop,), name="embedded-worker", daemon=True)
    thread.start()
    return thread, stop


def main() -> None:
    configure_logging(debug=get_settings().debug)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_forever(stop)


if __name__ == "__main__":
    main()
