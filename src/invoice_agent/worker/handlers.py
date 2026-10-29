"""What a worker does for each kind of job.

``process`` runs the agent graph for one document. The graph owns its own transactions (that is
what lets a run resume after a crash), so the session the worker opens for the job is only used
for the job's bookkeeping. A retried job continues the same run from its last checkpoint,
because the run id is the job id.

Three more kinds wake a run that waits for a supplier (ADR-0014): ``mail_poll`` looks at what
the mailroom received and queues a ``query_reply`` per reply, which resumes the run;
``query_deadline`` ends the wait when nobody answered. A job that finds the run no longer
waiting for it (the reply came first, or the deadline did) does nothing, and succeeds.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from invoice_agent.agent.correspondence.answers import WaitEnded
from invoice_agent.agent.correspondence.jobs import (
    MAIL_POLL,
    QUERY_DEADLINE,
    QUERY_REPLY,
    poll_bucket,
    poll_job,
    reply_job,
)
from invoice_agent.agent.failure import fail_invoice_of_run
from invoice_agent.agent.graph.run import Graph, answer_pause, run_status, start_run
from invoice_agent.agent.graph.state import PauseKind
from invoice_agent.config import MailSettings
from invoice_agent.db.repositories import JobRepository
from invoice_agent.domain.job import Job, PermanentJobError
from invoice_agent.observability.logging import log_event
from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolError
from invoice_agent.tools.mailroom import MailroomConnection, MailroomReader
from invoice_agent.tools.mcp_connection import CallPolicy
from mailroom.schemas import MAX_LISTED, MessageInfo

logger = logging.getLogger("invoice_agent.worker")

PROCESS = "process"


@dataclass(frozen=True)
class JobContext:
    session: Session
    now: datetime
    tracing: Tracing = field(default_factory=Tracing)
    agent: Graph | None = None
    """The compiled agent graph; ``process`` jobs cannot run without it."""
    mailroom: MCPServer | StdioServerParameters | None = None
    """Where to reach the mail provider; the poll has nothing to look at without it."""
    mailroom_policy: CallPolicy = field(default_factory=CallPolicy)
    mail: MailSettings = field(default_factory=MailSettings)
    max_attempts: int = 5
    """Attempts of the jobs this one queues."""


Handler = Callable[[Job, JobContext], None]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessPayload(_Payload):
    document: str


class QueryReplyPayload(_Payload):
    run_id: UUID
    message_id: UUID


class QueryDeadlinePayload(_Payload):
    run_id: UUID
    round: int


class MailPollPayload(_Payload):
    pass


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


def _agent(context: JobContext) -> Graph:
    if context.agent is None:
        raise PermanentJobError("no agent graph is configured for this worker")
    return context.agent


def handle_query_reply(job: Job, context: JobContext) -> None:
    """A supplier answered: resume the run that waits for it. Nothing to do if it does not."""
    payload = _payload(QueryReplyPayload, job)
    agent = _agent(context)
    if run_status(agent, payload.run_id).pending_kind != PauseKind.AWAIT_REPLY.value:
        # Not an error: the reply is in the conversation, the person reads it from there.
        log_event(logger, "reply_not_awaited", run_id=str(payload.run_id))
        return
    answer_pause(
        agent,
        payload.run_id,
        PauseKind.AWAIT_REPLY,
        WaitEnded(event="reply", message_id=payload.message_id),
    )


def handle_query_deadline(job: Job, context: JobContext) -> None:
    """The time is up for one message: end the wait, unless the reply (or a later round) did."""
    payload = _payload(QueryDeadlinePayload, job)
    agent = _agent(context)
    status = run_status(agent, payload.run_id)
    if (
        status.pending_kind != PauseKind.AWAIT_REPLY.value
        or (status.pending or {}).get("round") != payload.round
    ):
        log_event(logger, "deadline_stale", run_id=str(payload.run_id), round=payload.round)
        return
    answer_pause(agent, payload.run_id, PauseKind.AWAIT_REPLY, WaitEnded(event="timeout"))


PAGES = 10
PAGE = MAX_LISTED


async def _replies_since(context: JobContext, since: datetime) -> list[MessageInfo]:
    """Every reply received after ``since``, page by page (bounded)."""
    assert context.mailroom is not None
    found: list[MessageInfo] = []
    async with MailroomConnection(
        context.mailroom, policy=context.mailroom_policy, tracing=context.tracing
    ) as connection:
        reader = MailroomReader(connection)
        cursor: datetime | None = since
        for _ in range(PAGES):
            page = await reader.list_inbound(since=cursor, limit=PAGE)
            found.extend(page)
            if len(page) < PAGE:
                break
            cursor = page[-1].created_at
    return found


def handle_mail_poll(job: Job, context: JobContext) -> None:
    """Queue a ``query_reply`` for every reply of the last days, and the next poll.

    A poll is periodic and best effort: when the mailroom cannot be reached it logs and
    succeeds, so the next poll is queued all the same. (A job that failed would have its whole
    transaction rolled back, the next poll with it, and a few failures would end the chain.)
    Replies already seen are queued again and ignored (one job per message, by key), so no
    memory of what was seen is needed.
    """
    _payload(MailPollPayload, job)
    if context.mailroom is None:
        raise PermanentJobError("no mailroom is configured for this worker")
    jobs = JobRepository(context.session)
    interval = context.mail.poll_interval_s
    jobs.enqueue(
        poll_job(
            poll_bucket(context.now, interval) + 1,
            interval_s=interval,
            now=context.now,
            max_attempts=context.max_attempts,
        )
    )
    since = context.now - timedelta(days=context.mail.lookback_days)
    try:
        replies = asyncio.run(_replies_since(context, since))
    except ToolError as exc:
        log_event(logger, "mail_poll_failed", level=logging.WARNING, error=str(exc))
        return
    for reply in replies:
        jobs.enqueue(
            reply_job(reply.thread_id, reply.id, now=context.now, max_attempts=context.max_attempts)
        )
    log_event(logger, "mail_polled", replies=len(replies))


HANDLERS: Mapping[str, Handler] = {
    PROCESS: handle_process,
    QUERY_REPLY: handle_query_reply,
    QUERY_DEADLINE: handle_query_deadline,
    MAIL_POLL: handle_mail_poll,
}


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
