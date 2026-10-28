from datetime import UTC, datetime
from uuid import uuid4

from invoice_agent.agent.graph.progress import (
    EXPLAIN,
    QUEUED,
    READ,
    READY,
    Stage,
    StageState,
    progress_of,
)
from invoice_agent.domain.audit import ActorType, AuditEvent
from invoice_agent.domain.job import Job, JobStatus

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
RUN = uuid4()
INVOICE = uuid4()


def job(status: JobStatus = JobStatus.RUNNING, error: str | None = None) -> Job:
    running = status is JobStatus.RUNNING
    return Job(
        id=RUN,
        kind="process",
        status=status,
        attempts=1 if status is not JobStatus.QUEUED else 0,
        run_after=NOW,
        locked_by="w" if running else None,
        locked_until=NOW if running else None,
        last_error=error,
        created_at=NOW,
        updated_at=NOW,
    )


def event(step: str, payload: dict[str, object] | None = None) -> AuditEvent:
    return AuditEvent(
        id=uuid4(),
        run_id=RUN,
        step=step,
        actor_type=ActorType.SYSTEM,
        actor_id="test",
        invoice_id=INVOICE if step != "none" else None,
        payload=payload or {},  # type: ignore[arg-type]
        occurred_at=NOW,
    )


def states(stages: tuple[Stage, ...]) -> dict[str, str]:
    return {s.key: s.state.value for s in stages}


def test_a_queued_job_waits_in_the_queue() -> None:
    progress = progress_of(job(JobStatus.QUEUED), [], NOW)
    assert states(progress.stages) == {
        "queued": "active",
        "read": "pending",
        "check": "pending",
        "propose": "pending",
        "ready": "pending",
    }
    assert not progress.finished and progress.invoice_id is None


def test_a_job_that_started_is_out_of_the_queue_and_reading() -> None:
    progress = progress_of(job(), [], NOW)
    assert states(progress.stages)["queued"] == "done"
    assert states(progress.stages)["read"] == "active"


def test_each_event_advances_the_active_stage() -> None:
    after_receive = progress_of(job(), [event("receive")], NOW)
    assert states(after_receive.stages)["read"] == "done"
    assert states(after_receive.stages)["check"] == "active"
    both = [event("receive"), event("validate", {"anomalies": []})]
    after_validate = progress_of(job(), both, NOW)
    assert states(after_validate.stages)["check"] == "done"
    assert states(after_validate.stages)["propose"] == "active"
    assert after_validate.invoice_id == INVOICE


def test_the_explain_stage_exists_only_when_the_checks_found_something() -> None:
    clean = progress_of(job(), [event("receive"), event("validate", {"anomalies": []})], NOW)
    assert EXPLAIN not in [s.key for s in clean.stages]
    found = progress_of(
        job(),
        [event("receive"), event("validate", {"anomalies": [{"code": "PO_NOT_FOUND"}]})],
        NOW,
    )
    assert states(found.stages)[EXPLAIN] == "active"


def test_a_later_event_proves_the_earlier_stages_even_without_an_explain_event() -> None:
    events = [
        event("receive"),
        event("validate", {"anomalies": [{"code": "PO_NOT_FOUND"}]}),
        event("propose"),
    ]
    progress = progress_of(job(), events, NOW)
    assert states(progress.stages)[EXPLAIN] == "done"
    assert states(progress.stages)["propose"] == "done"


def test_a_succeeded_run_that_reached_the_proposal_is_ready_for_a_decision() -> None:
    events = [event("receive"), event("validate", {"anomalies": []}), event("propose")]
    progress = progress_of(job(JobStatus.SUCCEEDED), events, NOW)
    assert states(progress.stages)[READY] == "done"
    assert progress.finished and not progress.failed and not progress.decided


def test_a_decision_is_reported() -> None:
    events = [
        event("receive"),
        event("validate", {"anomalies": []}),
        event("propose"),
        event("approval"),
    ]
    assert progress_of(job(JobStatus.SUCCEEDED), events, NOW).decided


def test_a_dead_job_fails_the_stage_it_was_in_and_keeps_the_reason() -> None:
    progress = progress_of(job(JobStatus.DEAD, "document unavailable: not found"), [], NOW)
    assert states(progress.stages)[QUEUED] == "done"  # it did start
    assert states(progress.stages)[READ] == "failed"
    assert states(progress.stages)["check"] == "pending"
    assert progress.finished and progress.failed
    assert progress.error == "document unavailable: not found"


def test_a_dead_job_after_reading_fails_the_checks_stage() -> None:
    progress = progress_of(job(JobStatus.DEAD, "boom"), [event("receive")], NOW)
    assert states(progress.stages)[READ] == "done"
    assert states(progress.stages)["check"] == "failed"


def test_a_succeeded_run_that_stopped_early_is_finished_not_spinning() -> None:
    progress = progress_of(job(JobStatus.SUCCEEDED), [event("receive")], NOW)
    assert progress.finished
    assert states(progress.stages)["check"] == StageState.SKIPPED.value


def test_the_error_is_only_reported_for_a_dead_job() -> None:
    assert progress_of(job(JobStatus.RUNNING, "earlier attempt failed"), [], NOW).error is None


def test_a_job_queued_for_long_enough_is_reported_as_stalled() -> None:
    from datetime import timedelta

    queued = job(JobStatus.QUEUED)
    assert not progress_of(queued, [], NOW + timedelta(seconds=5)).stalled
    assert progress_of(queued, [], NOW + timedelta(seconds=60)).stalled
    assert not progress_of(job(), [], NOW + timedelta(seconds=60)).stalled  # running is not stalled
