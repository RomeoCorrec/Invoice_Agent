import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.config import WorkerSettings
from invoice_agent.db.repositories import JobRepository
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.job import Job, JobStatus, PermanentJobError
from invoice_agent.worker.handlers import Handler, JobContext
from invoice_agent.worker.runner import Worker

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
SETTINGS = WorkerSettings(
    poll_interval_s=0.05, job_timeout_s=10, max_attempts=3, backoff_base_s=5, backoff_cap_s=600
)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_worker(
    factory: sessionmaker[Session],
    handlers: dict[str, Handler],
    clock: Callable[[], datetime],
    tmp_path: Path,
    worker_id: str = "w1",
    settings: WorkerSettings = SETTINGS,
) -> Worker:
    def context_for(session: Session, now: datetime) -> JobContext:
        return JobContext(session=session, now=now)

    return Worker(
        factory,
        worker_id=worker_id,
        handlers=handlers,
        context_for=context_for,
        settings=settings,
        clock=clock,
        rng=lambda: 0.0,  # the shortest allowed delay: half the ceiling
    )


def enqueue(factory: sessionmaker[Session], kind: str = "work", **overrides: Any) -> Job:
    job = Job(
        **{
            "id": uuid4(),
            "kind": kind,
            "payload": {},
            "max_attempts": SETTINGS.max_attempts,
            "run_after": T0,
            "created_at": T0,
            "updated_at": T0,
            **overrides,
        }
    )
    with session_scope(factory) as session:
        JobRepository(session).enqueue(job)
    return job


def load(factory: sessionmaker[Session], job: Job) -> Job:
    with session_scope(factory) as session:
        loaded = JobRepository(session).get(job.id)
    assert loaded is not None
    return loaded


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_a_successful_job_is_marked_succeeded(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    seen: list[str] = []
    job = enqueue(factory)
    worker = make_worker(factory, {"work": lambda j, _c: seen.append(j.kind)}, clock, tmp_path)
    assert worker.run_until_idle() == 1
    done = load(factory, job)
    assert (done.status, done.attempts) == (JobStatus.SUCCEEDED, 1)
    assert seen == ["work"]


def test_nothing_due_means_nothing_done(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    worker = make_worker(factory, {}, clock, tmp_path)
    assert worker.run_once() is False


def test_a_transient_failure_is_retried_after_a_backoff(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    calls: list[int] = []

    def flaky(job: Job, _context: JobContext) -> None:
        calls.append(job.attempts)
        if len(calls) < 3:
            raise ConnectionError("database unavailable")

    job = enqueue(factory)
    worker = make_worker(factory, {"work": flaky}, clock, tmp_path)

    assert worker.run_once() is True  # attempt 1 fails
    queued = load(factory, job)
    assert queued.status is JobStatus.QUEUED
    assert queued.run_after == T0 + timedelta(seconds=2.5)  # half of the 5 s ceiling
    assert "ConnectionError" in (queued.last_error or "")
    assert worker.run_once() is False  # not due yet: the backoff is respected

    clock.advance(timedelta(seconds=2.5))
    assert worker.run_once() is True  # attempt 2 fails, the delay doubles
    assert load(factory, job).run_after == clock.now + timedelta(seconds=5)

    clock.advance(timedelta(seconds=5))
    assert worker.run_once() is True  # attempt 3 succeeds
    done = load(factory, job)
    assert (done.status, done.attempts, calls) == (JobStatus.SUCCEEDED, 3, [1, 2, 3])


def test_a_job_that_keeps_failing_ends_up_dead(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    def always_fails(_job: Job, _context: JobContext) -> None:
        raise RuntimeError("provider down")

    job = enqueue(factory)
    worker = make_worker(factory, {"work": always_fails}, clock, tmp_path)
    for _ in range(SETTINGS.max_attempts):
        assert worker.run_once() is True
        clock.advance(timedelta(minutes=30))
    dead = load(factory, job)
    assert (dead.status, dead.attempts) == (JobStatus.DEAD, SETTINGS.max_attempts)
    assert "provider down" in (dead.last_error or "")
    assert worker.run_once() is False  # a dead job is never claimed again


def test_a_permanent_failure_is_not_retried(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    def refuses(_job: Job, _context: JobContext) -> None:
        raise PermanentJobError("malformed payload")

    job = enqueue(factory)
    worker = make_worker(factory, {"work": refuses}, clock, tmp_path)
    worker.run_once()
    dead = load(factory, job)
    assert (dead.status, dead.attempts) == (JobStatus.DEAD, 1)


def test_an_unknown_kind_goes_dead_at_once(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    job = enqueue(factory, kind="mystery")
    make_worker(factory, {}, clock, tmp_path).run_once()
    dead = load(factory, job)
    assert dead.status is JobStatus.DEAD
    assert "unknown job kind" in (dead.last_error or "")


def test_what_a_failed_job_wrote_is_rolled_back(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    side_effect = Job(
        id=uuid4(),
        kind="side",
        payload={},
        run_after=T0 + timedelta(days=1),
        created_at=T0,
        updated_at=T0,
    )

    def writes_then_fails(_job: Job, context: JobContext) -> None:
        JobRepository(context.session).enqueue(side_effect)
        raise RuntimeError("crash after writing")

    job = enqueue(factory)
    make_worker(factory, {"work": writes_then_fails}, clock, tmp_path).run_once()
    assert load(factory, job).status is JobStatus.QUEUED
    with session_scope(factory) as session:
        assert JobRepository(session).get(side_effect.id) is None


def test_a_worker_that_lost_its_lease_leaves_no_trace(
    factory: sessionmaker[Session], engine: Engine, clock: Clock, tmp_path: Path
) -> None:
    """Another worker took the job over during a long pause: our work must not be committed."""
    side_effect = Job(
        id=uuid4(), kind="side", payload={}, run_after=T0, created_at=T0, updated_at=T0
    )

    def slow(job: Job, context: JobContext) -> None:
        JobRepository(context.session).enqueue(side_effect)
        with engine.begin() as connection:  # the other worker takes over meanwhile
            connection.execute(
                text("UPDATE jobs SET locked_by = 'other' WHERE id = :id"), {"id": job.id}
            )

    job = enqueue(factory)
    make_worker(factory, {"work": slow}, clock, tmp_path).run_once()
    taken = load(factory, job)
    assert (taken.status, taken.locked_by) == (JobStatus.RUNNING, "other")
    with session_scope(factory) as session:
        assert JobRepository(session).get(side_effect.id) is None


def test_a_job_abandoned_by_a_dead_worker_is_taken_over(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    job = enqueue(factory)
    with session_scope(factory) as session:  # worker A claims, then dies without a word
        JobRepository(session).claim("dead", now=T0, lease=timedelta(seconds=SETTINGS.lease_s))
    done: list[str] = []
    worker = make_worker(factory, {"work": lambda j, _c: done.append("b")}, clock, tmp_path, "b")
    assert worker.run_once() is False  # the lease is still valid
    clock.advance(timedelta(seconds=SETTINGS.lease_s + 1))
    assert worker.run_once() is True
    taken = load(factory, job)
    assert (taken.status, taken.attempts, done) == (JobStatus.SUCCEEDED, 2, ["b"])


def test_two_workers_process_every_job_exactly_once(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    jobs = [enqueue(factory, payload={"n": n}) for n in range(16)]
    processed: list[str] = []
    lock = threading.Lock()

    def record(job: Job, _context: JobContext) -> None:
        with lock:
            processed.append(str(job.id))

    workers = [
        make_worker(factory, {"work": record}, clock, tmp_path, worker_id=f"w{n}") for n in range(2)
    ]
    threads = [threading.Thread(target=w.run_until_idle) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(processed) == sorted(str(j.id) for j in jobs)
    assert {load(factory, j).status for j in jobs} == {JobStatus.SUCCEEDED}


def test_a_stop_request_lets_the_current_job_finish(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(_job: Job, _context: JobContext) -> None:
        started.set()
        release.wait(timeout=30)

    first, second = enqueue(factory), enqueue(factory)
    worker = make_worker(factory, {"work": slow}, clock, tmp_path)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    assert started.wait(timeout=30)
    worker.request_stop()  # asked while the job is running
    release.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    statuses = sorted(load(factory, j).status.value for j in (first, second))
    assert statuses == ["queued", "succeeded"]  # the running job finished, no new one started


def test_an_idle_worker_stops_promptly(
    factory: sessionmaker[Session], clock: Clock, tmp_path: Path
) -> None:
    worker = make_worker(factory, {}, clock, tmp_path)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
