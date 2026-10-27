"""Start, continue and resume agent runs.

A run is identified by its run id, which is also the checkpoint thread id. ``start_run`` is
safe to call again for the same run (a retried job): it continues where the checkpoint left
off, and does nothing if the run is already waiting for a human or finished.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.nodes import REVIEWABLE, STEP_RECEIVE
from invoice_agent.agent.graph.state import AgentState, ApprovalDecision, Verdict
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import session_scope

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


class NoPendingApprovalError(LookupError):
    """There is nothing to approve: the run is unknown, finished, or not waiting."""


class CannotApproveError(ValueError):
    """An approval was asked for an invoice that has no entry to post."""


@dataclass(frozen=True)
class RunStatus:
    run_id: UUID
    finished: bool
    awaiting_approval: dict[str, Any] | None
    """The request shown to the reviewer while the run is waiting, otherwise ``None``."""
    state: AgentState


def _config(run_id: UUID) -> RunnableConfig:
    return RunnableConfig(configurable={"thread_id": str(run_id)})


def run_status(graph: Graph, run_id: UUID) -> RunStatus:
    snapshot = graph.get_state(_config(run_id))
    pending = [i.value for task in snapshot.tasks for i in task.interrupts]
    return RunStatus(
        run_id=run_id,
        finished=bool(snapshot.values) and not snapshot.next,
        awaiting_approval=pending[0] if pending else None,
        state=snapshot.values,  # type: ignore[arg-type]
    )


def start_run(
    graph: Graph, *, run_id: UUID, document: str, trace_id: str | None = None
) -> RunStatus:
    """Run the graph until it finishes or waits for a human; continue it if it was started."""
    config = _config(run_id)
    current = run_status(graph, run_id)
    if not current.state:
        graph.invoke({"document": document, "run_id": str(run_id), "trace_id": trace_id}, config)
    elif not current.finished and current.awaiting_approval is None:
        graph.invoke(None, config)  # a node failed or the process died: continue from there
    return run_status(graph, run_id)


def run_id_for_invoice(session: Session, invoice_id: UUID) -> UUID | None:
    """The run that ingested an invoice, from the audit trail."""
    for event in AuditEventRepository(session).list_for_invoice(invoice_id):
        if event.step == STEP_RECEIVE:
            return event.run_id
    return None


def resume_run(
    graph: Graph,
    session_factory: sessionmaker[Session],
    invoice_id: UUID,
    decision: ApprovalDecision,
) -> RunStatus:
    """Give the waiting run its human decision. Refuses when no run is waiting."""
    with session_scope(session_factory) as session:
        run_id = run_id_for_invoice(session, invoice_id)
    request = run_status(graph, run_id).awaiting_approval if run_id else None
    if run_id is None or request is None:
        raise NoPendingApprovalError(f"no run is waiting for a decision on invoice {invoice_id}")
    if decision.verdict is Verdict.APPROVE and request.get("entry") is None:
        # Refused before the run resumes: approving would leave an approved invoice that can
        # never be posted, and the answer cannot be taken back once the run has consumed it.
        blocked = request.get("entry_blocked") or {}
        raise CannotApproveError(
            f"invoice {invoice_id} has no entry to post ({blocked.get('reason', 'unknown')}): "
            "it can only be rejected"
        )
    graph.invoke(Command(resume=decision.model_dump(mode="json")), _config(run_id))
    return run_status(graph, run_id)


def pending_approvals(graph: Graph, session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    """The requests of the runs that are waiting for a human, oldest invoice first.

    An invoice can be ``validated`` without a run waiting on it (it was handled before the
    graph existed, or its run is gone): it is not listed, because it could not be decided.
    """
    with session_scope(session_factory) as session:
        invoices = InvoiceRepository(session)
        candidates = [i for status in REVIEWABLE for i in invoices.list_by_status(status)]
        runs = [run_id_for_invoice(session, invoice.id) for invoice in candidates]
    requests = []
    for run_id in runs:
        request = run_status(graph, run_id).awaiting_approval if run_id else None
        if request is not None:
            requests.append(request)
    return requests


def pending_request(
    graph: Graph, session_factory: sessionmaker[Session], invoice_id: UUID
) -> dict[str, Any] | None:
    """What the run of this invoice is asking the reviewer, or ``None`` if it is not waiting."""
    with session_scope(session_factory) as session:
        run_id = run_id_for_invoice(session, invoice_id)
    return run_status(graph, run_id).awaiting_approval if run_id else None
