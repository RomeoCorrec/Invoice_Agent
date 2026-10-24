from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invoice_agent.db.repositories import JobRepository, LeaseLostError
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.job import InvalidJobTransition, Job, JobStatus

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
LEASE = timedelta(minutes=2)


def make_job(**overrides: Any) -> Job:
    values: dict[str, Any] = {
        "id": uuid4(),
        "kind": "ingest",
        "payload": {"uri": "a.pdf"},
        "run_after": T0,
        "created_at": T0,
        "updated_at": T0,
    }
    return Job(**{**values, **overrides})


def test_job_round_trip(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job(idempotency_key="k1")
    repo.enqueue(job)
    session.expunge_all()
    assert repo.get(job.id) == job


def test_enqueue_with_the_same_key_returns_the_existing_job(engine: Engine) -> None:
    factory = make_session_factory(engine)
    first = make_job(idempotency_key="validate:1")
    with session_scope(factory) as session:
        JobRepository(session).enqueue(first)
    with session_scope(factory) as session:
        again = JobRepository(session).enqueue(make_job(idempotency_key="validate:1"))
        assert again.id == first.id
    with session_scope(factory) as session:
        assert sum(JobRepository(session).count_by_status().values()) == 1


def test_jobs_without_a_key_are_never_deduplicated(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue(make_job())
    repo.enqueue(make_job())
    assert repo.count_by_status() == {JobStatus.QUEUED: 2}


def test_claim_takes_the_oldest_due_job_and_leases_it(session: Session) -> None:
    repo = JobRepository(session)
    later = make_job(run_after=T0 + timedelta(seconds=5))
    sooner = make_job(run_after=T0 - timedelta(minutes=1))
    repo.enqueue(later)
    repo.enqueue(sooner)
    claimed = repo.claim("w1", now=T0 + timedelta(seconds=10), lease=LEASE)
    assert claimed is not None
    assert claimed.id == sooner.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_by == "w1"
    assert claimed.locked_until == T0 + timedelta(seconds=10) + LEASE


def test_a_job_that_is_not_due_yet_is_not_claimed(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue(make_job(run_after=T0 + timedelta(minutes=5)))
    assert repo.claim("w1", now=T0, lease=LEASE) is None


def test_two_workers_never_claim_the_same_job(engine: Engine) -> None:
    """Worker A holds its claim open (uncommitted); worker B must skip that row, not wait."""
    factory = make_session_factory(engine)
    with session_scope(factory) as setup:
        JobRepository(setup).enqueue(make_job(payload={"n": 1}))
        JobRepository(setup).enqueue(make_job(payload={"n": 2}))

    worker_a, worker_b = factory(), factory()
    try:
        first = JobRepository(worker_a).claim("a", now=T0, lease=LEASE)
        second = JobRepository(worker_b).claim("b", now=T0, lease=LEASE)
        third = JobRepository(worker_b).claim("b", now=T0, lease=LEASE)
        assert first is not None
        assert second is not None
        assert first.id != second.id
        assert third is None  # both jobs are taken
        worker_a.commit()
        worker_b.commit()
    finally:
        worker_a.close()
        worker_b.close()


def test_a_job_whose_lease_expired_is_taken_over(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        JobRepository(session).enqueue(make_job())
    with session_scope(factory) as session:
        assert JobRepository(session).claim("dead-worker", now=T0, lease=LEASE) is not None
    with session_scope(factory) as session:  # still within the lease
        assert (
            JobRepository(session).claim("w2", now=T0 + timedelta(minutes=1), lease=LEASE) is None
        )
    with session_scope(factory) as session:  # lease over
        taken = JobRepository(session).claim("w2", now=T0 + timedelta(minutes=3), lease=LEASE)
    assert taken is not None
    assert taken.locked_by == "w2"
    assert taken.attempts == 2


def test_a_job_taken_over_with_no_attempt_left_goes_dead(engine: Engine) -> None:
    factory = make_session_factory(engine)
    job = make_job(max_attempts=1)
    with session_scope(factory) as session:
        JobRepository(session).enqueue(job)
        assert JobRepository(session).claim("w1", now=T0, lease=LEASE) is not None
    with session_scope(factory) as session:
        assert (
            JobRepository(session).claim("w2", now=T0 + timedelta(minutes=5), lease=LEASE) is None
        )
    with session_scope(factory) as session:
        dead = JobRepository(session).get(job.id)
    assert dead is not None
    assert dead.status is JobStatus.DEAD
    assert "died" in (dead.last_error or "")


def test_succeed_clears_the_lease(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job()
    repo.enqueue(job)
    claimed = repo.claim("w1", now=T0, lease=LEASE)
    assert claimed is not None
    repo.succeed(job.id, "w1", now=T0 + timedelta(seconds=3))
    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.locked_by is None
    assert repo.claim("w1", now=T0 + timedelta(hours=1), lease=LEASE) is None


def test_retry_requeues_for_later_and_keeps_the_error(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job()
    repo.enqueue(job)
    repo.claim("w1", now=T0, lease=LEASE)
    later = T0 + timedelta(minutes=1)
    repo.retry(job.id, "w1", now=T0, run_after=later, error="database unavailable")
    queued = repo.get(job.id)
    assert queued is not None
    assert (queued.status, queued.attempts, queued.run_after) == (JobStatus.QUEUED, 1, later)
    assert queued.last_error == "database unavailable"
    assert repo.claim("w1", now=T0 + timedelta(seconds=30), lease=LEASE) is None
    again = repo.claim("w1", now=later, lease=LEASE)
    assert again is not None
    assert again.attempts == 2


def test_die_is_final(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job()
    repo.enqueue(job)
    repo.claim("w1", now=T0, lease=LEASE)
    repo.die(job.id, "w1", now=T0, error="unknown job kind")
    dead = repo.get(job.id)
    assert dead is not None
    assert dead.status is JobStatus.DEAD
    assert repo.claim("w1", now=T0 + timedelta(days=1), lease=LEASE) is None


def test_only_the_owner_can_finish_a_job(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job()
    repo.enqueue(job)
    repo.claim("w1", now=T0, lease=LEASE)
    with pytest.raises(LeaseLostError):
        repo.succeed(job.id, "intruder", now=T0)
    with pytest.raises(LeaseLostError):
        repo.succeed(uuid4(), "w1", now=T0)


def test_a_finished_job_cannot_be_finished_again(session: Session) -> None:
    repo = JobRepository(session)
    job = make_job()
    repo.enqueue(job)
    repo.claim("w1", now=T0, lease=LEASE)
    repo.succeed(job.id, "w1", now=T0)
    with pytest.raises((LeaseLostError, InvalidJobTransition)):
        repo.die(job.id, "w1", now=T0, error="late")


def test_the_database_refuses_an_inconsistent_lease(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="lease"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts, run_after, "
                "created_at, updated_at) "
                "VALUES (:id, 'x', '{}', 'running', 1, 5, now(), now(), now())"
            ),
            {"id": uuid4()},
        )


def test_the_database_bounds_attempts(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="attempts_bounded"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts, run_after, "
                "created_at, updated_at) "
                "VALUES (:id, 'x', '{}', 'queued', 6, 5, now(), now(), now())"
            ),
            {"id": uuid4()},
        )
