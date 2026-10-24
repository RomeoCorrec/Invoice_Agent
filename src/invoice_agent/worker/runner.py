"""The worker loop: claim a job, run its handler, record the outcome.

Three short transactions per job, never one long one:

1. claim: lock the next due job, mark it running with a lease, commit;
2. work: run the handler and mark the job succeeded, in one transaction, so the effects of
   the job and its completion are committed together or not at all;
3. on failure: roll back the work, then in a new transaction either schedule a retry with
   exponential backoff or, when attempts are exhausted or the failure is permanent, mark the
   job dead.

Delivery is at least once. If a worker dies, its lease expires and another takes the job over;
if the old worker comes back from a long pause, its completion fails on the lease check and
its work is rolled back.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.config import WorkerSettings
from invoice_agent.db.repositories import JobRepository, LeaseLostError
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import redact_payload
from invoice_agent.domain.job import Job, PermanentJobError, RetryPolicy
from invoice_agent.observability.logging import log_event
from invoice_agent.worker.handlers import Handler, JobContext

logger = logging.getLogger("invoice_agent.worker")

MAX_ERROR_LENGTH = 500


def utc_now() -> datetime:
    return datetime.now(UTC)


def describe_error(exc: BaseException) -> str:
    """A short, IBAN-free description that is safe to store and log."""
    text = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    redacted = redact_payload(text)
    assert isinstance(redacted, str)
    return redacted


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        worker_id: str,
        handlers: Mapping[str, Handler],
        context_for: Callable[[Session, datetime], JobContext],
        settings: WorkerSettings,
        clock: Callable[[], datetime] = utc_now,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._factory = factory
        self._worker_id = worker_id
        self._handlers = handlers
        self._context_for = context_for
        self._settings = settings
        self._clock = clock
        self._policy = RetryPolicy(
            max_attempts=settings.max_attempts,
            base=timedelta(seconds=settings.backoff_base_s),
            cap=timedelta(seconds=settings.backoff_cap_s),
        )
        self._rng = rng
        self._stop = threading.Event()

    # -- control ------------------------------------------------------------------------

    def request_stop(self) -> None:
        """Finish the job in progress, if any, then leave the loop."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        def stop(signum: int, _frame: FrameType | None) -> None:
            log_event(logger, "stop_requested", signal=signal.Signals(signum).name)
            self.request_stop()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    def run_forever(self) -> None:
        log_event(logger, "worker_started", worker=self._worker_id, kinds=sorted(self._handlers))
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self._settings.poll_interval_s)
        log_event(logger, "worker_stopped", worker=self._worker_id)

    def run_until_idle(self) -> int:
        """Process jobs until none is due (for tests and one-shot runs); returns how many."""
        processed = 0
        while not self._stop.is_set() and self.run_once():
            processed += 1
        return processed

    # -- one job ------------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and process one job. False if none was due."""
        lease = timedelta(seconds=self._settings.lease_s)
        with session_scope(self._factory) as session:
            job = JobRepository(session).claim(self._worker_id, now=self._clock(), lease=lease)
        if job is None:
            return False
        self._process(job)
        return True

    def _process(self, job: Job) -> None:
        started = time.monotonic()
        fields: dict[str, Any] = {
            "job_id": str(job.id),
            "kind": job.kind,
            "attempt": job.attempts,
        }
        log_event(logger, "job_started", worker=self._worker_id, **fields)
        try:
            handler = self._handlers.get(job.kind)
            if handler is None:
                raise PermanentJobError(f"unknown job kind {job.kind!r}")
            with session_scope(self._factory) as session:
                now = self._clock()
                handler(job, self._context_for(session, now))
                JobRepository(session).succeed(job.id, self._worker_id, now=now)
        except LeaseLostError:
            log_event(logger, "lease_lost", level=logging.WARNING, **fields)
            return
        except PermanentJobError as exc:
            self._record_failure(job, describe_error(exc), permanent=True, fields=fields)
            return
        except Exception as exc:
            self._record_failure(job, describe_error(exc), permanent=False, fields=fields)
            return
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        log_event(logger, "job_succeeded", duration_ms=duration_ms, **fields)

    def _record_failure(
        self, job: Job, error: str, *, permanent: bool, fields: dict[str, Any]
    ) -> None:
        now = self._clock()
        exhausted = job.attempts >= job.max_attempts
        try:
            with session_scope(self._factory) as session:
                repo = JobRepository(session)
                if permanent or exhausted:
                    repo.die(job.id, self._worker_id, now=now, error=error)
                    log_event(logger, "job_dead", level=logging.ERROR, error=error, **fields)
                else:
                    delay = (
                        self._policy.delay(job.attempts, self._rng)
                        if self._rng
                        else self._policy.delay(job.attempts)
                    )
                    repo.retry(job.id, self._worker_id, now=now, run_after=now + delay, error=error)
                    log_event(
                        logger,
                        "job_retry",
                        level=logging.WARNING,
                        error=error,
                        retry_in_s=round(delay.total_seconds(), 1),
                        **fields,
                    )
        except LeaseLostError:
            log_event(logger, "lease_lost", level=logging.WARNING, **fields)
