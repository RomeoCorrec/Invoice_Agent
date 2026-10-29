"""The jobs the correspondence uses to hear from the outside and to know when time is up.

A run that waits for a supplier is not a process that sleeps: it is a pause in the database.
Two things wake it, and both are jobs of the queue the worker already runs (ADR-0005, ADR-0014):

* a **reply** (``query_reply``), found by a **poll** (``mail_poll``) that looks at what the
  mailroom received and queues one job per message;
* a **deadline** (``query_deadline``), queued when the message is sent, due when the supplier
  has had long enough.

Each job has an idempotency key, so a poll that sees the same reply twice, or a node that
runs again, queues it once.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from invoice_agent.domain.job import Job

QUERY_REPLY = "query_reply"
QUERY_DEADLINE = "query_deadline"
MAIL_POLL = "mail_poll"


def reply_job(run_id: UUID, message_id: UUID, *, now: datetime, max_attempts: int = 5) -> Job:
    """Resume the run waiting in ``run_id`` with this reply (one job per message)."""
    return Job(
        id=uuid4(),
        kind=QUERY_REPLY,
        payload={"run_id": str(run_id), "message_id": str(message_id)},
        max_attempts=max_attempts,
        run_after=now,
        idempotency_key=f"reply:{message_id}",
        created_at=now,
        updated_at=now,
    )


def deadline_job(
    run_id: UUID, round_no: int, *, run_after: datetime, now: datetime, max_attempts: int = 5
) -> Job:
    """End the wait of ``run_id`` for the message of this round, if nobody answered by then."""
    return Job(
        id=uuid4(),
        kind=QUERY_DEADLINE,
        payload={"run_id": str(run_id), "round": round_no},
        max_attempts=max_attempts,
        run_after=run_after,
        idempotency_key=f"deadline:{run_id}:{round_no}",
        created_at=now,
        updated_at=now,
    )


def poll_bucket(now: datetime, interval_s: int) -> int:
    """The number of the time slot ``now`` is in: one poll per slot, whoever queues it."""
    return int(now.timestamp() // interval_s)


def poll_job(bucket: int, *, interval_s: int, now: datetime, max_attempts: int = 5) -> Job:
    """The poll of a time slot, due at the start of the slot (immediately if it has begun)."""
    start = datetime.fromtimestamp(bucket * interval_s, UTC)
    return Job(
        id=uuid4(),
        kind=MAIL_POLL,
        payload={},
        max_attempts=max_attempts,
        run_after=max(start, now),
        idempotency_key=f"mail_poll:{bucket}",
        created_at=now,
        updated_at=now,
    )
