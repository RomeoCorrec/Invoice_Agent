from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.domain.job import (
    ALLOWED_TRANSITIONS,
    InvalidJobTransition,
    Job,
    JobStatus,
    RetryPolicy,
    check_job_transition,
)

J = JobStatus
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

# Written out by hand on purpose: deriving it from the table would make the test circular.
EXPECTED_ALLOWED = {
    (J.QUEUED, J.RUNNING),
    (J.RUNNING, J.SUCCEEDED),
    (J.RUNNING, J.QUEUED),
    (J.RUNNING, J.DEAD),
}
FORBIDDEN = [pair for pair in product(J, J) if pair not in EXPECTED_ALLOWED]


def make_job(**overrides: object) -> Job:
    values: dict[str, object] = {
        "id": uuid4(),
        "kind": "ingest",
        "run_after": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Job(**{**values, **overrides})  # type: ignore[arg-type]


def test_table_matches_the_expected_transitions() -> None:
    actual = {(src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in targets}
    assert actual == EXPECTED_ALLOWED


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED_ALLOWED))
def test_allowed_transition(current: JobStatus, target: JobStatus) -> None:
    check_job_transition(current, target)


@pytest.mark.parametrize(("current", "target"), FORBIDDEN)
def test_forbidden_transition(current: JobStatus, target: JobStatus) -> None:
    with pytest.raises(InvalidJobTransition):
        check_job_transition(current, target)


def test_finished_jobs_never_come_back() -> None:
    assert ALLOWED_TRANSITIONS[J.SUCCEEDED] == frozenset()
    assert ALLOWED_TRANSITIONS[J.DEAD] == frozenset()


def test_a_running_job_needs_a_worker_and_a_lease() -> None:
    with pytest.raises(ValidationError, match="lease"):
        make_job(status=J.RUNNING)
    job = make_job(status=J.RUNNING, locked_by="w1", locked_until=NOW + timedelta(minutes=2))
    assert job.locked_by == "w1"


def test_only_a_running_job_holds_a_lease() -> None:
    with pytest.raises(ValidationError, match="only a running"):
        make_job(status=J.QUEUED, locked_by="w1", locked_until=NOW)


def test_attempts_cannot_exceed_the_maximum() -> None:
    with pytest.raises(ValidationError, match="attempts"):
        make_job(attempts=6, max_attempts=5)


# --- retry policy ----------------------------------------------------------------------------


def test_backoff_ceiling_doubles_then_is_capped() -> None:
    policy = RetryPolicy()
    seconds = [policy.ceiling(n).total_seconds() for n in range(1, 10)]
    assert seconds == [5, 10, 20, 40, 80, 160, 320, 600, 600]


def test_delay_is_between_half_the_ceiling_and_the_ceiling() -> None:
    policy = RetryPolicy()
    assert policy.delay(3, rng=lambda: 0.0) == timedelta(seconds=10)
    assert policy.delay(3, rng=lambda: 1.0) == timedelta(seconds=20)
    for _ in range(200):
        assert timedelta(seconds=10) <= policy.delay(3) <= timedelta(seconds=20)


def test_a_retry_is_never_immediate() -> None:
    assert RetryPolicy().delay(1, rng=lambda: 0.0) > timedelta(0)


def test_attempts_start_at_one() -> None:
    with pytest.raises(ValueError, match="starts at 1"):
        RetryPolicy().ceiling(0)
