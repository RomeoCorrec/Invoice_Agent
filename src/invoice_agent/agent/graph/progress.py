"""What a run has done so far, for the page that follows an upload.

The run id is the id of the job that started it, and every node writes an audit event when it
is done. So the progress is read, not guessed: a stage is done when the event of that stage,
or of a later one, exists. No timer, no state of its own.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from invoice_agent.agent.graph.entry_nodes import STEP_PROPOSE
from invoice_agent.agent.graph.explain_node import STEP_EXPLAIN, STEP_EXPLAIN_UNAVAILABLE
from invoice_agent.agent.graph.nodes import STEP_APPROVAL, STEP_RECEIVE, STEP_VALIDATE
from invoice_agent.db.repositories import AuditEventRepository, JobRepository
from invoice_agent.domain.audit import AuditEvent
from invoice_agent.domain.job import Job, JobStatus

QUEUED, READ, CHECK, EXPLAIN, PROPOSE, READY = (
    "queued",
    "read",
    "check",
    "explain",
    "propose",
    "ready",
)
STALLED_AFTER_SECONDS = 15
LABELS = {
    QUEUED: "In the queue",
    READ: "Reading the document",
    CHECK: "Checking the figures",
    EXPLAIN: "Explaining the anomalies",
    PROPOSE: "Proposing the accounting entry",
    READY: "Ready for your decision",
}


class StageState(StrEnum):
    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    state: StageState


@dataclass(frozen=True)
class Progress:
    stages: tuple[Stage, ...]
    finished: bool
    failed: bool
    invoice_id: UUID | None
    decided: bool
    error: str | None
    job_status: JobStatus
    stalled: bool
    """Queued for a while: most likely no worker is running."""


def _anomaly_codes(events: Sequence[AuditEvent]) -> list[str] | None:
    """The codes the checks found, or ``None`` while the checks have not run."""
    for event in events:
        if event.step == STEP_VALIDATE:
            anomalies = event.payload.get("anomalies", [])
            if not isinstance(anomalies, list):
                return []
            return [str(a["code"]) for a in anomalies if isinstance(a, dict) and "code" in a]
    return None


def progress_of(job: Job, events: Sequence[AuditEvent], now: datetime) -> Progress:
    steps = {event.step for event in events}
    anomalies = _anomaly_codes(events)
    keys = [QUEUED, READ, CHECK]
    if anomalies:  # the explain stage exists only when there is something to explain
        keys.append(EXPLAIN)
    keys += [PROPOSE, READY]
    succeeded = job.status is JobStatus.SUCCEEDED
    evidence = {
        QUEUED: job.status is not JobStatus.QUEUED or bool(steps),
        READ: STEP_RECEIVE in steps,
        CHECK: STEP_VALIDATE in steps,
        EXPLAIN: bool(steps & {STEP_EXPLAIN, STEP_EXPLAIN_UNAVAILABLE}),
        PROPOSE: STEP_PROPOSE in steps,
        READY: STEP_PROPOSE in steps and succeeded,
    }
    # Evidence of a later stage proves the earlier ones: the explain node may not be configured.
    reached = max((i for i, key in enumerate(keys) if evidence[key]), default=-1)
    dead = job.status is JobStatus.DEAD
    finished = dead or (succeeded and reached == len(keys) - 1)
    ended_early = succeeded and not finished

    stages: list[Stage] = []
    for i, key in enumerate(keys):
        if i <= reached:
            state = StageState.DONE
        elif i == reached + 1 and dead:
            state = StageState.FAILED
        elif i == reached + 1 and not ended_early:
            state = StageState.ACTIVE
        elif ended_early:
            state = StageState.SKIPPED
        else:
            state = StageState.PENDING
        stages.append(Stage(key, LABELS[key], state))
    invoice_id = next((e.invoice_id for e in events if e.step == STEP_RECEIVE), None)
    return Progress(
        stages=tuple(stages),
        finished=finished or ended_early,
        failed=dead,
        invoice_id=invoice_id,
        decided=STEP_APPROVAL in steps,
        error=job.last_error if dead else None,
        job_status=job.status,
        stalled=job.status is JobStatus.QUEUED
        and (now - job.created_at).total_seconds() > STALLED_AFTER_SECONDS,
    )


def run_progress(session: Session, job_id: UUID, kind: str, now: datetime) -> Progress | None:
    """The progress of the run started by this job, or ``None`` if there is no such job."""
    job = JobRepository(session).get(job_id)
    if job is None or job.kind != kind:
        return None
    return progress_of(job, AuditEventRepository(session).list_for_run(job_id), now)
