"""What a worker does for each kind of job.

There is one kind, ``process``: it runs the agent graph for one document. The graph owns its
own transactions (that is what lets a run resume after a crash), so the session the worker
opens for the job is only used for the job's bookkeeping. A retried job continues the same run
from its last checkpoint, because the run id is the job id.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from invoice_agent.agent.failure import fail_invoice_of_run
from invoice_agent.agent.graph.run import Graph, start_run
from invoice_agent.domain.job import Job, PermanentJobError
from invoice_agent.observability.tracing import Tracing

PROCESS = "process"


@dataclass(frozen=True)
class JobContext:
    session: Session
    now: datetime
    tracing: Tracing = field(default_factory=Tracing)
    agent: Graph | None = None
    """The compiled agent graph; ``process`` jobs cannot run without it."""


Handler = Callable[[Job, JobContext], None]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessPayload(_Payload):
    document: str


def _payload[P: _Payload](model: type[P], job: Job) -> P:
    try:
        return model.model_validate(job.payload)
    except ValidationError as exc:
        raise PermanentJobError(f"malformed {job.kind!r} payload") from exc


def handle_process(job: Job, context: JobContext) -> None:
    """Run the agent on one document, up to the human approval or the end of the run."""
    payload = _payload(ProcessPayload, job)
    if context.agent is None:
        raise PermanentJobError("no agent graph is configured for this worker")
    start_run(
        context.agent,
        run_id=job.id,
        document=payload.document,
        trace_id=context.tracing.current_trace_id(),
    )


HANDLERS: Mapping[str, Handler] = {PROCESS: handle_process}


def on_job_dead(session: Session, job: Job, now: datetime, error: str) -> None:
    """A document the agent gave up on must not stay half-processed: its invoice fails."""
    if job.kind == PROCESS:
        fail_invoice_of_run(session, job.id, now=now, reason=error)


def process_job(document: str, *, now: datetime, max_attempts: int = 5) -> Job:
    """The job that processes one document of the store (its name is the idempotency key)."""
    return Job(
        id=uuid4(),
        kind=PROCESS,
        payload={"document": document},
        max_attempts=max_attempts,
        run_after=now,
        idempotency_key=f"{PROCESS}:{document}",
        created_at=now,
        updated_at=now,
    )
