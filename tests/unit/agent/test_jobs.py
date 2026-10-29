from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from invoice_agent.agent.correspondence.jobs import (
    MAIL_POLL,
    QUERY_DEADLINE,
    QUERY_REPLY,
    deadline_job,
    poll_bucket,
    poll_job,
    reply_job,
)
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.worker.handlers import (
    HANDLERS,
    JobContext,
    handle_mail_poll,
    handle_query_deadline,
    handle_query_reply,
)

NOW = datetime(2026, 10, 29, 12, 0, 30, tzinfo=UTC)


def test_a_reply_job_is_one_per_message_and_due_at_once() -> None:
    run, message = uuid4(), uuid4()
    job = reply_job(run, message, now=NOW)
    assert job.kind == QUERY_REPLY and job.run_after == NOW
    assert job.payload == {"run_id": str(run), "message_id": str(message)}
    assert job.idempotency_key == f"reply:{message}"
    assert reply_job(run, message, now=NOW).idempotency_key == job.idempotency_key  # same key


def test_a_deadline_job_is_due_later_and_named_by_run_and_round() -> None:
    run = uuid4()
    due = NOW + timedelta(days=3)
    job = deadline_job(run, 2, run_after=due, now=NOW)
    assert job.kind == QUERY_DEADLINE and job.run_after == due
    assert job.payload == {"run_id": str(run), "round": 2}
    assert job.idempotency_key == f"deadline:{run}:2"
    assert deadline_job(run, 1, run_after=due, now=NOW).idempotency_key != job.idempotency_key


def test_polls_are_numbered_by_time_slot_and_one_is_due_per_slot() -> None:
    interval = 60
    bucket = poll_bucket(NOW, interval)
    assert poll_bucket(NOW + timedelta(seconds=29), interval) == bucket  # same minute
    assert poll_bucket(NOW + timedelta(seconds=31), interval) == bucket + 1
    now_job = poll_job(bucket, interval_s=interval, now=NOW)
    next_job = poll_job(bucket + 1, interval_s=interval, now=NOW)
    assert now_job.kind == MAIL_POLL and now_job.run_after == NOW  # the slot has begun: now
    assert next_job.run_after == datetime(2026, 10, 29, 12, 1, tzinfo=UTC)  # the slot to come
    assert {now_job.idempotency_key, next_job.idempotency_key} == {
        f"mail_poll:{bucket}",
        f"mail_poll:{bucket + 1}",
    }


def test_the_worker_knows_the_three_new_kinds() -> None:
    assert {QUERY_REPLY, QUERY_DEADLINE, MAIL_POLL} <= set(HANDLERS)


def context(**changes: object) -> JobContext:
    return JobContext(session=MagicMock(spec=Session), now=NOW, **changes)  # type: ignore[arg-type]


def test_a_job_with_an_unexpected_payload_is_refused_for_good() -> None:
    odd = reply_job(uuid4(), uuid4(), now=NOW).model_copy(update={"payload": {"run_id": "x"}})
    with pytest.raises(PermanentJobError, match="malformed"):
        handle_query_reply(odd, context(agent=MagicMock()))
    extra = deadline_job(uuid4(), 1, run_after=NOW, now=NOW).model_copy(
        update={"payload": {"run_id": str(uuid4()), "round": 1, "why": "because"}}
    )
    with pytest.raises(PermanentJobError, match="malformed"):
        handle_query_deadline(extra, context(agent=MagicMock()))


def test_the_handlers_refuse_to_run_without_what_they_need() -> None:
    reply = reply_job(uuid4(), uuid4(), now=NOW)
    with pytest.raises(PermanentJobError, match="no agent graph"):
        handle_query_reply(reply, context())
    with pytest.raises(PermanentJobError, match="no mailroom"):
        handle_mail_poll(poll_job(1, interval_s=60, now=NOW), context())
