"""Proposing and posting the accounting entry: what the reviewer sees is what the ledger gets,
and posting is done once however the run is interrupted."""

from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph import entry_nodes
from invoice_agent.agent.graph.entry_nodes import posting_key
from invoice_agent.agent.graph.run import CannotApproveError, Graph, resume_run, start_run
from invoice_agent.db.repositories import AuditEventRepository, JournalEntryRepository
from invoice_agent.db.session import session_scope
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.tools.errors import ToolTransientError
from ledger import tables
from ledger.schemas import EntryRequest
from ledger.server import build_server
from ledger.store import LedgerStore
from tests.integration.support import (
    APPROVE,
    REJECT,
    document,
    graph_for,
    start,
    status_of,
    steps,
)


def ledger_entries(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.entries)).scalar_one()


def waiting(graph: Graph, documents: Path, case: str) -> tuple[Any, UUID]:
    run = start(graph, document(documents, case))
    return run, UUID(run.state["invoice_id"])


def test_the_posted_entry_is_the_one_the_reviewer_was_shown(
    graph: Graph, factory: sessionmaker[Session], documents: Path, engine: Engine
) -> None:
    run, invoice_id = waiting(graph, documents, "INV-01")
    shown = run.awaiting_approval["entry"]
    resume_run(graph, factory, invoice_id, APPROVE)

    assert status_of(factory, str(invoice_id)) is InvoiceStatus.POSTED
    assert ledger_entries(engine) == 1
    with engine.connect() as connection:
        lines = connection.execute(
            select(
                tables.entry_lines.c.account, tables.entry_lines.c.side, tables.entry_lines.c.amount
            ).order_by(tables.entry_lines.c.position)
        ).all()
    assert [(a, s, str(m)) for a, s, m in lines] == [
        (line["account"], line["side"], line["amount"]) for line in shown["lines"]
    ]
    # the agent keeps a copy, with the ledger's own id and the posting key
    with session_scope(factory) as session:
        local = JournalEntryRepository(session).get_by_invoice(invoice_id)
    assert local is not None and local.idempotency_key == posting_key(invoice_id)
    assert local.total_debit == local.total_credit == Decimal("360.00")


def test_a_rejected_invoice_is_never_posted(
    graph: Graph, factory: sessionmaker[Session], documents: Path, engine: Engine
) -> None:
    run, invoice_id = waiting(graph, documents, "INV-12")
    resume_run(graph, factory, invoice_id, REJECT)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.REJECTED
    assert ledger_entries(engine) == 0
    assert "post" not in steps(factory, run.run_id)


def test_a_crash_between_the_ledger_and_the_database_posts_once(
    graph_factory: Any,
    factory: sessionmaker[Session],
    documents: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger took the entry, the process died before the invoice was marked posted."""
    real = entry_nodes._record_posted
    crashed = [False]

    def crash_first_time(*args: Any) -> None:
        if not crashed[0]:
            crashed[0] = True
            raise RuntimeError("the process died after the ledger answered")
        real(*args)

    monkeypatch.setattr(entry_nodes, "_record_posted", crash_first_time)
    graph = graph_factory()
    run, invoice_id = waiting(graph, documents, "INV-01")
    with pytest.raises(RuntimeError, match="died"):
        resume_run(graph, factory, invoice_id, APPROVE)
    assert ledger_entries(engine) == 1
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED  # the job would retry

    start_run(graph, run_id=run.run_id, document=document(documents, "INV-01"))
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.POSTED
    assert ledger_entries(engine) == 1  # the retry was a replay, not a second entry
    with session_scope(factory) as session:
        post = [
            e for e in AuditEventRepository(session).list_for_run(run.run_id) if e.step == "post"
        ]
    assert len(post) == 1 and post[0].payload["replayed"] is True


def test_the_ledger_refusing_the_entry_fails_the_invoice_with_the_code(
    graph: Graph, factory: sessionmaker[Session], documents: Path, engine: Engine
) -> None:
    run, invoice_id = waiting(graph, documents, "INV-01")
    # someone else booked this invoice number for this supplier in the meantime
    store = LedgerStore(engine)
    shown = run.awaiting_approval["entry"]
    store.post_entry(
        EntryRequest(
            idempotency_key="someone-else",
            supplier_siren=shown["supplier_siren"],
            invoice_number=shown["invoice_number"],
            entry_date=shown["entry_date"],
            lines=shown["lines"],
        )
    )
    resume_run(graph, factory, invoice_id, APPROVE)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.FAILED
    assert ledger_entries(engine) == 1
    with session_scope(factory) as session:
        events = AuditEventRepository(session).list_for_invoice(invoice_id)
    assert events[-1].step == "post" and events[-1].payload["outcome"] == "refused"
    assert "invoice_already_booked" in str(events[-1].payload["reason"])


def test_an_unreachable_ledger_stops_the_run_and_a_retry_posts(
    graph_factory: Any,
    factory: sessionmaker[Session],
    documents: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = entry_nodes._post_to_ledger
    calls = [0]

    async def down_then_up(deps: Any, request: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise ToolTransientError("could not connect to the ledger")
        return await real(deps, request)

    monkeypatch.setattr(entry_nodes, "_post_to_ledger", down_then_up)
    graph = graph_factory()
    run, invoice_id = waiting(graph, documents, "INV-01")
    with pytest.raises(ToolTransientError):
        resume_run(graph, factory, invoice_id, APPROVE)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED
    assert ledger_entries(engine) == 0

    start_run(graph, run_id=run.run_id, document=document(documents, "INV-01"))
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.POSTED
    assert ledger_entries(engine) == 1


def test_an_invoice_from_an_unknown_supplier_has_no_entry_and_cannot_be_approved(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    ledger_server: MCPServer,
    engine: Engine,
) -> None:
    # INV-09's supplier is not in the reference data once the suppliers are removed
    with session_scope(factory) as session:
        session.execute(text("TRUNCATE suppliers, purchase_orders CASCADE"))
    graph = graph_for(factory, documents, saver, ledger=ledger_server)
    run, invoice_id = waiting(graph, documents, "INV-09")

    request = run.awaiting_approval
    assert request["entry"] is None and request["entry_blocked"]["reason"] == "unknown_supplier"
    with pytest.raises(CannotApproveError, match="unknown_supplier"):
        resume_run(graph, factory, invoice_id, APPROVE)
    # the run is still waiting: the refused answer was not consumed
    resume_run(graph, factory, invoice_id, REJECT)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.REJECTED
    assert ledger_entries(engine) == 0


def test_a_run_without_a_ledger_cannot_post(
    factory: sessionmaker[Session], documents: Path, saver: PostgresSaver
) -> None:
    graph = graph_for(factory, documents, saver)  # no ledger configured
    _, invoice_id = waiting(graph, documents, "INV-01")
    with pytest.raises(PermanentJobError, match="no ledger"):
        resume_run(graph, factory, invoice_id, APPROVE)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED


def test_a_read_only_ledger_refuses_to_post(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    engine: Engine,
    ledger_server: MCPServer,
) -> None:
    graph = graph_for(
        factory, documents, saver, ledger=build_server(LedgerStore(engine), writable=False)
    )
    _, invoice_id = waiting(graph, documents, "INV-01")
    resume_run(graph, factory, invoice_id, APPROVE)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.FAILED
    assert ledger_entries(engine) == 0
