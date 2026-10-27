"""The nodes that turn an approved invoice into a posted entry.

``propose_entry`` runs before the human pause, so the reviewer sees the entry that would be
posted. ``post_entry`` runs only after an approval, with exactly that entry, and writes it
through the ledger's idempotent tool. The graph is synchronous and the ledger client is not:
the node bridges with ``asyncio.run``, which is safe because a worker thread has no running
event loop.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy.orm import Session

from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.nodes import Node
from invoice_agent.agent.graph.state import AgentState, invoice_id_of, run_id_of
from invoice_agent.db.repositories import (
    AuditEventRepository,
    InvoiceRepository,
    JournalEntryRepository,
    SupplierRepository,
)
from invoice_agent.db.session import session_scope
from invoice_agent.domain.accounting import EntryProposal, propose_entry
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.invoice import Invoice
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.domain.journal import JournalEntry
from invoice_agent.tools.errors import ToolRefusedError
from invoice_agent.tools.ledger import LedgerConnection, LedgerWriter
from ledger.schemas import EntryRequest, PostedEntry

STEP_PROPOSE = "propose"
STEP_POST = "post"
RULES_ACTOR = "accounting-rules"
LEDGER_ACTOR = "ledger"


def posting_key(invoice_id: UUID) -> str:
    """One invoice, one key: posting it again, from any retry, is the same posting."""
    return f"post:{invoice_id}"


def make_propose_entry(deps: AgentDeps) -> Node:
    def propose(state: AgentState) -> dict[str, Any]:
        """Build the entry for the reviewer, or say why none can be built."""
        run_id, invoice_id = run_id_of(state), invoice_id_of(state)
        with session_scope(deps.session_factory) as session:
            events = AuditEventRepository(session)
            recorded = next(
                (e for e in events.list_for_run(run_id) if e.step == STEP_PROPOSE), None
            )
            if recorded is not None:  # replay: the first answer stands
                return _state_fragment(recorded.payload)
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None or invoice.data is None:
                raise PermanentJobError(f"invoice {invoice_id} has no extracted data")
            supplier = (
                SupplierRepository(session).get(invoice.supplier_id)
                if invoice.supplier_id
                else None
            )
            result = propose_entry(invoice.data, supplier)
            payload: dict[str, JsonValue] = (
                {"entry": result.model_dump(mode="json")}
                if isinstance(result, EntryProposal)
                else {"blocked": result.model_dump(mode="json")}
            )
            if state.get("trace_id"):
                payload["trace_id"] = state["trace_id"]
            _audit(
                session,
                run_id,
                invoice_id,
                STEP_PROPOSE,
                ActorType.SYSTEM,
                RULES_ACTOR,
                payload,
                deps,
            )
        return _state_fragment(payload)

    return propose


def _state_fragment(payload: dict[str, JsonValue]) -> dict[str, Any]:
    entry, blocked = payload.get("entry"), payload.get("blocked")
    return {
        "entry": entry if isinstance(entry, dict) else None,
        "entry_blocked": blocked if isinstance(blocked, dict) else None,
    }


def make_post_entry(deps: AgentDeps) -> Node:
    def post(state: AgentState) -> dict[str, Any]:
        """Post the approved entry to the ledger, once, and mark the invoice posted."""
        invoice_id = invoice_id_of(state)
        with session_scope(deps.session_factory) as session:
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None:
                raise PermanentJobError(f"invoice {invoice_id} not found")
            if invoice.status is InvoiceStatus.POSTED:
                return {"status": InvoiceStatus.POSTED.value}  # replay: already done
            if invoice.status is not InvoiceStatus.APPROVED:
                raise PermanentJobError(
                    f"invoice {invoice_id} is {invoice.status.value!r}, only an approved invoice "
                    "can be posted"
                )
        proposal_json = state.get("entry")
        if proposal_json is None:
            raise PermanentJobError("an invoice with no proposed entry cannot be posted")
        if deps.ledger is None:
            raise PermanentJobError("no ledger is configured")
        proposal = EntryProposal.model_validate(proposal_json)
        request = EntryRequest(
            idempotency_key=posting_key(invoice_id),
            supplier_siren=proposal.supplier_siren,
            invoice_number=proposal.invoice_number,
            entry_date=proposal.entry_date,
            journal=proposal.journal,
            lines=proposal.lines,
        )
        try:
            posted = asyncio.run(_post_to_ledger(deps, request))
        except ToolRefusedError as refusal:
            _fail(deps, state, invoice, f"the ledger refused the entry ({refusal.code})")
            return {"status": InvoiceStatus.FAILED.value}
        _record_posted(deps, state, invoice, proposal, posted)
        return {"status": InvoiceStatus.POSTED.value}

    return post


async def _post_to_ledger(deps: AgentDeps, request: EntryRequest) -> PostedEntry:
    assert deps.ledger is not None
    async with LedgerConnection(
        deps.ledger, policy=deps.ledger_policy, tracing=deps.tracing
    ) as connection:
        return await LedgerWriter(connection).post_entry(request)


def _record_posted(
    deps: AgentDeps,
    state: AgentState,
    invoice: Invoice,
    proposal: EntryProposal,
    posted: PostedEntry,
) -> None:
    now = deps.clock()
    with session_scope(deps.session_factory) as session:
        JournalEntryRepository(session).add(
            JournalEntry(
                id=posted.entry_id,
                invoice_id=invoice.id,
                journal=proposal.journal,
                entry_date=proposal.entry_date,
                reference=proposal.invoice_number,
                idempotency_key=posted.idempotency_key,
                lines=proposal.lines,
                created_at=now,
            )
        )
        invoices = InvoiceRepository(session)
        invoices.save(
            invoice.transition_to(InvoiceStatus.POSTED, at=now),
            expected_status=InvoiceStatus.APPROVED,
        )
        payload: dict[str, JsonValue] = {
            "entry_id": str(posted.entry_id),
            "idempotency_key": posted.idempotency_key,
            "replayed": posted.replayed,
            "total": str(posted.total),
        }
        if state.get("trace_id"):
            payload["trace_id"] = state["trace_id"]
        _audit(
            session,
            run_id_of(state),
            invoice.id,
            STEP_POST,
            ActorType.TOOL,
            LEDGER_ACTOR,
            payload,
            deps,
        )


def _fail(deps: AgentDeps, state: AgentState, invoice: Invoice, reason: str) -> None:
    now = deps.clock()
    with session_scope(deps.session_factory) as session:
        InvoiceRepository(session).save(
            invoice.transition_to(InvoiceStatus.FAILED, at=now, failure_reason=reason),
            expected_status=InvoiceStatus.APPROVED,
        )
        payload: dict[str, JsonValue] = {"outcome": "refused", "reason": reason}
        if state.get("trace_id"):
            payload["trace_id"] = state["trace_id"]
        _audit(
            session,
            run_id_of(state),
            invoice.id,
            STEP_POST,
            ActorType.TOOL,
            LEDGER_ACTOR,
            payload,
            deps,
        )


def _audit(
    session: Session,
    run_id: UUID,
    invoice_id: UUID,
    step: str,
    actor_type: ActorType,
    actor_id: str,
    payload: dict[str, JsonValue],
    deps: AgentDeps,
) -> None:
    redacted = redact_payload(payload)
    assert isinstance(redacted, dict)
    AuditEventRepository(session).append(
        AuditEvent(
            id=uuid4(),
            run_id=run_id,
            step=step,
            actor_type=actor_type,
            actor_id=actor_id,
            invoice_id=invoice_id,
            payload=redacted,
            occurred_at=deps.clock(),
        )
    )
