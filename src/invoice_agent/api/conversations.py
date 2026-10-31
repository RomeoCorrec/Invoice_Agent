"""The conversations with suppliers, for the web interface (ADR-0014).

A conversation is a run that is in the correspondence subgraph. Which one, and what it waits
for, is read from the audit trail and the run's pause; the text of the messages is read from
the mailroom, never kept in the run.
"""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.query_nodes import STEP_CLOSED, STEP_DRAFTED
from invoice_agent.agent.graph.run import Graph, RunStatus, run_id_for_invoice, run_status
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import session_scope
from invoice_agent.tools.mailroom import MailroomConnection, MailroomReader
from invoice_agent.tools.mcp_connection import CallPolicy
from mailroom.schemas import MessageInfo

WAITS = {
    "review_draft": "A message to the supplier is waiting for your review",
    "await_reply": "Waiting for the supplier's answer",
    "review_reply": "The supplier answered: read it",
}


@dataclass(frozen=True)
class Conversation:
    run_id: UUID
    invoice_id: UUID
    invoice_number: str
    seller: str
    kind: str | None
    """What the run waits for, ``None`` when it waits for nothing (a step failed)."""
    round: int | None

    @property
    def label(self) -> str:
        return WAITS.get(self.kind or "", "Stopped on an error")


def open_conversations(graph: Graph, factory: sessionmaker[Session]) -> list[Conversation]:
    with session_scope(factory) as session:
        runs = AuditEventRepository(session).open_conversations(STEP_DRAFTED, STEP_CLOSED)
        invoices = InvoiceRepository(session)
        found = [(run, invoices.get(invoice)) for run, invoice in runs]
    conversations = []
    for run_id, invoice in found:
        if invoice is None or invoice.data is None:
            continue
        status = run_status(graph, run_id)
        if status.finished:
            continue
        conversations.append(
            conversation_of(
                run_id,
                invoice.id,
                invoice.data.invoice_number,
                invoice.data.seller.name,
                status,
            )
        )
    return conversations


def conversation_of(
    run_id: UUID, invoice_id: UUID, number: str, seller: str, status: RunStatus
) -> Conversation:
    pending = status.pending or {}
    return Conversation(
        run_id, invoice_id, number, seller, status.pending_kind, pending.get("round")
    )


def conversation_for_invoice(
    graph: Graph, factory: sessionmaker[Session], invoice_id: UUID
) -> tuple[Conversation, RunStatus] | None:
    """The open conversation of an invoice, with its run, or ``None``."""
    with session_scope(factory) as session:
        run_id = run_id_for_invoice(session, invoice_id)
        invoice = InvoiceRepository(session).get(invoice_id)
        open_runs = {
            run
            for run, _ in AuditEventRepository(session).open_conversations(
                STEP_DRAFTED, STEP_CLOSED
            )
        }
    if run_id is None or invoice is None or invoice.data is None or run_id not in open_runs:
        return None
    status = run_status(graph, run_id)
    if status.finished:
        return None
    return conversation_of(
        run_id, invoice_id, invoice.data.invoice_number, invoice.data.seller.name, status
    ), status


def read_thread(
    mailroom: MCPServer | StdioServerParameters | None, policy: CallPolicy, run_id: UUID
) -> list[MessageInfo] | None:
    """The messages of a conversation, oldest first; ``None`` if the mailroom is not available."""
    if mailroom is None:
        return None

    async def read() -> list[MessageInfo]:
        async with MailroomConnection(mailroom, policy=policy) as connection:
            return await MailroomReader(connection).get_thread(run_id)

    return asyncio.run(read())
