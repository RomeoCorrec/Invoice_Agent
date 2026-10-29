"""How a supplier's reply, and the lack of one, reach a run that waits (ADR-0014).

The poll queues a job per reply, the deadline job is queued when the message is sent; both are
jobs of the worker's queue, run here by a real worker on a clock the test moves.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.correspondence.answers import DraftReview
from invoice_agent.agent.correspondence.jobs import poll_bucket, poll_job
from invoice_agent.agent.graph.deps import QueryConfig
from invoice_agent.agent.graph.run import Graph, RunStatus, answer_pause, resume_run, run_status
from invoice_agent.agent.graph.state import ApprovalDecision, PauseKind, Verdict
from invoice_agent.config import MailSettings, WorkerSettings
from invoice_agent.db.reference_data import demo_contact_address
from invoice_agent.db.repositories import JobRepository
from invoice_agent.db.session import session_scope
from invoice_agent.tools.mcp_connection import CallPolicy
from invoice_agent.worker.handlers import HANDLERS, JobContext, on_job_dead
from invoice_agent.worker.runner import Worker
from invoice_agent.worker.store import LocalDocumentStore
from mailroom.seed import seed_contacts
from mailroom.server import build_server
from mailroom.store import MailroomStore
from tests.integration.support import agent_graph, lookup_extractor, start

ADDRESS = demo_contact_address("Café des Ateliers SARL")  # the seller of INV-12
SETTINGS = WorkerSettings(poll_interval_s=0.05, job_timeout_s=60, max_attempts=3)
MAIL = MailSettings(reply_deadline_days=3, poll_interval_s=60, lookback_days=7)
START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
ASK = ApprovalDecision(verdict=Verdict.REJECT_AND_QUERY, approver="alice")


class Clock:
    """A clock the test moves: the nodes and the worker read the same one."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(engine: Engine, factory: sessionmaker[Session]) -> Iterator[MailroomStore]:
    seed_contacts(engine)
    yield MailroomStore(engine)


@pytest.fixture
def server(store: MailroomStore) -> MCPServer:
    return build_server(store)


@pytest.fixture
def graph(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    server: MCPServer,
    clock: Clock,
) -> Graph:
    return agent_graph(
        saver,
        factory,
        LocalDocumentStore(documents),
        lookup_extractor(),
        clock=clock,
        mailroom=server,
        query=QueryConfig(reply_deadline=timedelta(days=3)),
    )


def worker_for(
    factory: sessionmaker[Session], graph: Graph, server: MCPServer, clock: Clock
) -> Worker:
    def context_for(session: Session, now: datetime) -> JobContext:
        return JobContext(
            session=session,
            now=now,
            agent=graph,
            mailroom=server,
            mailroom_policy=CallPolicy(attempts=1),
            mail=MAIL,
            max_attempts=SETTINGS.max_attempts,
        )

    return Worker(
        factory,
        worker_id="w1",
        handlers=HANDLERS,
        context_for=context_for,
        settings=SETTINGS,
        clock=clock,
        on_dead=on_job_dead,
    )


@pytest.fixture
def worker(factory: sessionmaker[Session], graph: Graph, server: MCPServer, clock: Clock) -> Worker:
    return worker_for(factory, graph, server, clock)


def waiting_for_a_reply(graph: Graph, factory: sessionmaker[Session], documents: Path) -> RunStatus:
    """A run on INV-12, rejected with a query and sent: it waits for the supplier."""
    name = next(p.name for p in documents.glob("INV-12_*.pdf"))
    run = start(graph, name)
    run = resume_run(graph, factory, UUID(run.state["invoice_id"]), ASK)
    return answer_pause(
        graph, run.run_id, PauseKind.REVIEW_DRAFT, DraftReview(action="send", approver="alice")
    )


def jobs(engine: Engine, kind: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT kind, status, run_after, idempotency_key, payload FROM jobs"
    with engine.connect() as connection:
        rows = connection.execute(text(query + " ORDER BY created_at, idempotency_key")).mappings()
        return [dict(row) for row in rows if kind is None or row["kind"] == kind]


def start_polling(factory: sessionmaker[Session], clock: Clock) -> None:
    with session_scope(factory) as session:
        JobRepository(session).enqueue(
            poll_job(poll_bucket(clock.now, MAIL.poll_interval_s), interval_s=60, now=clock.now)
        )


# --- the deadline ----------------------------------------------------------------------------


def test_sending_queues_the_deadline_of_that_message(
    graph: Graph, factory: sessionmaker[Session], documents: Path, engine: Engine, clock: Clock
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    [deadline] = jobs(engine, "query_deadline")
    assert deadline["status"] == "queued"
    assert deadline["run_after"] == START + timedelta(days=3)
    assert deadline["idempotency_key"] == f"deadline:{run.run_id}:1"
    assert deadline["payload"] == {"run_id": str(run.run_id), "round": 1}


def test_a_deadline_that_is_not_due_yet_changes_nothing(
    graph: Graph, factory: sessionmaker[Session], documents: Path, worker: Worker, clock: Clock
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    clock.advance(days=2)
    assert worker.run_until_idle() == 0  # nothing is due
    assert run_status(graph, run.run_id).pending_kind == "await_reply"


def test_the_deadline_ends_the_wait_and_a_reminder_is_drafted(
    graph: Graph, factory: sessionmaker[Session], documents: Path, worker: Worker, clock: Clock
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    clock.advance(days=3, minutes=1)
    assert worker.run_until_idle() == 1
    after = run_status(graph, run.run_id)
    assert after.pending_kind == "review_draft" and after.pending is not None
    assert after.pending["round"] == 2 and after.pending["reminder"] is True


# --- the replies -----------------------------------------------------------------------------


def test_a_poll_turns_a_reply_into_a_job_that_resumes_the_waiting_run(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    reply = store.receive_message(run.run_id, ADDRESS, "Re: facture", "Voici la facture.")
    start_polling(factory, clock)
    assert worker.run_until_idle() == 2  # the poll, then the reply it queued
    after = run_status(graph, run.run_id)
    assert after.pending_kind == "review_reply" and after.pending is not None
    assert after.pending["message_id"] == str(reply.id) and after.pending["from_contact"] is True
    [queued] = jobs(engine, "query_reply")
    assert queued["status"] == "succeeded" and queued["idempotency_key"] == f"reply:{reply.id}"
    [next_poll] = [j for j in jobs(engine, "mail_poll") if j["status"] == "queued"]
    assert next_poll["run_after"] == datetime(2026, 10, 29, 9, 1, tzinfo=UTC)  # the next minute


def test_seeing_the_same_reply_again_queues_nothing_and_changes_nothing(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    store.receive_message(run.run_id, ADDRESS, "Re:", "Voici la facture.")
    start_polling(factory, clock)
    worker.run_until_idle()
    clock.advance(minutes=1, seconds=1)
    assert worker.run_until_idle() == 1  # the next poll sees the same reply: one job, by key
    assert len(jobs(engine, "query_reply")) == 1
    assert run_status(graph, run.run_id).pending_kind == "review_reply"


def test_a_reply_that_arrives_when_nobody_waits_for_it_is_left_in_the_conversation(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    clock.advance(days=3, minutes=1)
    worker.run_until_idle()  # the deadline: a reminder is being drafted, nobody waits
    store.receive_message(run.run_id, ADDRESS, "Re:", "Désolé du retard, voici la facture.")
    start_polling(factory, clock)
    worker.run_until_idle()
    [queued] = jobs(engine, "query_reply")
    assert queued["status"] == "succeeded"  # not an error, nothing to redo
    assert run_status(graph, run.run_id).pending_kind == "review_draft"  # still the reminder
    assert [m.direction for m in store.get_thread(run.run_id)] == ["out", "in"]


def test_a_deadline_that_comes_after_the_reply_does_nothing(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    store.receive_message(run.run_id, ADDRESS, "Re:", "Voici la facture.")
    start_polling(factory, clock)
    worker.run_until_idle()
    assert run_status(graph, run.run_id).pending_kind == "review_reply"
    clock.advance(days=4)
    worker.run_until_idle()  # the deadline is due now, and finds nobody waiting for it
    assert run_status(graph, run.run_id).pending_kind == "review_reply"
    [deadline] = jobs(engine, "query_deadline")
    assert deadline["status"] == "succeeded"  # a stale deadline is not a failure to retry


def test_a_poll_finds_a_reply_that_arrived_while_the_worker_was_away(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    store.receive_message(run.run_id, ADDRESS, "Re:", "Voici la facture.")
    clock.advance(days=2)  # nobody polled for two days
    start_polling(factory, clock)
    worker.run_until_idle()
    assert run_status(graph, run.run_id).pending_kind == "review_reply"


def test_a_reply_older_than_the_lookback_is_not_picked_up_again(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
) -> None:
    run = waiting_for_a_reply(graph, factory, documents)
    store.receive_message(run.run_id, ADDRESS, "Re:", "Voici la facture.")
    clock.advance(days=8)  # beyond the seven days a poll looks back
    start_polling(factory, clock)
    worker.run_until_idle()
    assert jobs(engine, "query_reply") == []


def test_replies_are_read_page_by_page(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    worker: Worker,
    store: MailroomStore,
    engine: Engine,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("invoice_agent.worker.handlers.PAGE", 2)
    run = waiting_for_a_reply(graph, factory, documents)
    sent = [
        store.receive_message(run.run_id, ADDRESS, f"Re: {n}", f"Message {n}") for n in range(5)
    ]
    start_polling(factory, clock)
    worker.run_until_idle()
    keys = {j["idempotency_key"] for j in jobs(engine, "query_reply")}
    assert keys == {f"reply:{m.id}" for m in sent}  # five replies, pages of two


# --- the chain of polls ----------------------------------------------------------------------


def test_a_poll_that_fails_does_not_end_the_chain(
    factory: sessionmaker[Session],
    graph: Graph,
    clock: Clock,
    engine: Engine,
) -> None:
    broken = MCPServer("down")  # a mailroom that has no tool at all: every call fails
    unlucky = worker_for(factory, graph, broken, clock)
    start_polling(factory, clock)
    assert unlucky.run_until_idle() == 1
    polls = {j["idempotency_key"]: j["status"] for j in jobs(engine, "mail_poll")}
    slot = poll_bucket(clock.now, MAIL.poll_interval_s)
    # this poll succeeded having found nothing, and the next slot is queued: the chain goes on
    assert polls == {f"mail_poll:{slot}": "succeeded", f"mail_poll:{slot + 1}": "queued"}
    assert jobs(engine, "query_reply") == []


def test_starting_many_workers_queues_one_poll_per_slot(
    factory: sessionmaker[Session], engine: Engine, clock: Clock
) -> None:
    for _ in range(5):
        start_polling(factory, clock)
    assert len(jobs(engine, "mail_poll")) == 1


def test_a_run_waiting_for_a_reply_survives_a_dead_worker_between_poll_and_reply(
    graph: Graph,
    factory: sessionmaker[Session],
    documents: Path,
    server: MCPServer,
    store: MailroomStore,
    clock: Clock,
) -> None:
    """The reply job is a job like the others: another worker takes it over."""
    run = waiting_for_a_reply(graph, factory, documents)
    store.receive_message(run.run_id, ADDRESS, "Re:", "Voici la facture.")
    start_polling(factory, clock)
    first = worker_for(factory, graph, server, clock)
    assert first.run_once()  # the poll only: the reply job is queued, not yet run
    second = worker_for(factory, graph, server, clock)  # a different worker finishes the story
    assert second.run_once()
    assert run_status(graph, run.run_id).pending_kind == "review_reply"
