"""The nodes of the agent graph.

Each node is a thin wrapper around a service that already exists, with its own transaction,
and can be run again without doing its work twice: a crash can happen after a node committed
and before the checkpoint was written, and the run then replays that node. Replays are
recognised through the run id, which tags every audit event the run writes.
"""

from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.types import interrupt
from pydantic import JsonValue
from sqlalchemy.orm import Session

from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.state import (
    AgentState,
    ApprovalDecision,
    Verdict,
    invoice_id_of,
    run_id_of,
)
from invoice_agent.agent.ingestion import ingest_document
from invoice_agent.agent.validation import NotReadyForValidationError, validate_invoice
from invoice_agent.db.repositories import (
    AuditEventRepository,
    InvoiceNotFoundError,
    InvoiceRepository,
)
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.invoice import Invoice
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.ingestion.document import ExtractionError

STEP_RECEIVE = "receive"
STEP_VALIDATE = "validate"
STEP_APPROVAL = "approval"


class Node(Protocol):
    """A graph node: the signature LangGraph expects (a parameter named ``state``)."""

    def __call__(self, state: AgentState) -> dict[str, Any]: ...


REVIEWABLE = frozenset({InvoiceStatus.VALIDATED, InvoiceStatus.NEEDS_REVIEW})


class StaleApprovalError(RuntimeError):
    """The invoice is no longer in a state a decision can be applied to."""


def make_ingest(deps: AgentDeps) -> Node:
    def ingest(state: AgentState) -> dict[str, Any]:
        """Read the document and store it as an invoice (extracted or failed)."""
        run_id = run_id_of(state)
        with session_scope(deps.session_factory) as session:
            events = AuditEventRepository(session).list_for_run(run_id)
            replayed = next((e.invoice_id for e in events if e.step == STEP_RECEIVE), None)
            existing = InvoiceRepository(session).get(replayed) if replayed else None
        if existing is not None:
            return {"invoice_id": str(existing.id), "status": existing.status.value}

        try:
            content = deps.store.read(state["document"], deps.limits.max_document_bytes)
        except ExtractionError as exc:
            raise PermanentJobError(f"document unavailable: {exc}") from exc
        # The extraction can be slow (an LLM call): no transaction is open while it runs, the
        # session below is only used once the extraction is done.
        with session_scope(deps.session_factory) as session:
            invoice = ingest_document(
                session,
                content,
                state["document"],
                extractor=deps.extractor,
                iban_key=deps.iban_key,
                limits=deps.limits,
                run_id=run_id,
                now=deps.clock(),
                reader=deps.reader,
                trace_id=state.get("trace_id"),
            )
        extracted = invoice.status is InvoiceStatus.EXTRACTED
        deps.tracing.score("extraction_ok", extracted, comment=invoice.failure_reason)
        return {"invoice_id": str(invoice.id), "status": invoice.status.value}

    return ingest


def make_validate(deps: AgentDeps) -> Node:
    def validate(state: AgentState) -> dict[str, Any]:
        """Run the deterministic checks and match the purchase order."""
        run_id, invoice_id = run_id_of(state), invoice_id_of(state)
        with session_scope(deps.session_factory) as session:
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None:
                raise PermanentJobError(f"invoice {invoice_id} not found")
            if invoice.status in REVIEWABLE:
                # Replay of a node that had committed: report what it recorded.
                return {
                    "status": invoice.status.value,
                    "anomaly_codes": _recorded_anomaly_codes(session, run_id),
                }
            try:
                result = validate_invoice(
                    session,
                    invoice_id,
                    run_id=run_id,
                    now=deps.clock(),
                    trace_id=state.get("trace_id"),
                )
            except (InvoiceNotFoundError, NotReadyForValidationError) as exc:
                raise PermanentJobError(str(exc)) from exc
        deps.tracing.score("anomaly_count", len(result.anomalies))
        return {
            "status": result.status.value,
            "anomaly_codes": [anomaly.code.value for anomaly in result.anomalies],
        }

    return validate


def _recorded_anomaly_codes(session: Session, run_id: UUID) -> list[str]:
    for event in AuditEventRepository(session).list_for_run(run_id):
        if event.step == STEP_VALIDATE:
            anomalies = event.payload.get("anomalies", [])
            if isinstance(anomalies, list):
                return [str(a["code"]) for a in anomalies if isinstance(a, dict) and "code" in a]
    raise PermanentJobError("the invoice was validated outside this run")


def approval_request(
    invoice: Invoice,
    anomaly_codes: list[str],
    entry: dict[str, Any] | None = None,
    entry_blocked: dict[str, Any] | None = None,
    explanation: dict[str, Any] | None = None,
) -> dict[str, JsonValue]:
    """What a reviewer is shown: figures, anomaly codes and the entry that would be posted,
    never the document text."""
    data = invoice.data
    assert data is not None
    return {
        "invoice_id": str(invoice.id),
        "invoice_number": data.invoice_number,
        "seller": data.seller.name,
        "currency": data.currency,
        "total_ht": str(data.total_ht),
        "total_vat": str(data.total_vat),
        "total_ttc": str(data.total_ttc),
        "status": invoice.status.value,
        "anomaly_codes": list(anomaly_codes),
        "entry": entry,
        "entry_blocked": entry_blocked,
        "explanation": explanation,
    }


def make_request_approval(deps: AgentDeps) -> Node:
    def request_approval(state: AgentState) -> dict[str, Any]:
        """Stop the run until a human decides. Only reads: it runs again on resume."""
        with session_scope(deps.session_factory) as session:
            invoice = InvoiceRepository(session).get(invoice_id_of(state))
        if invoice is None:
            raise PermanentJobError("invoice not found")
        answer = interrupt(
            approval_request(
                invoice,
                state.get("anomaly_codes", []),
                state.get("entry"),
                state.get("entry_blocked"),
                state.get("explanation"),
            )
        )
        decision = ApprovalDecision.model_validate(answer)
        return {"decision": decision.model_dump(mode="json")}

    return request_approval


def make_apply_decision(deps: AgentDeps) -> Node:
    def apply_decision(state: AgentState) -> dict[str, Any]:
        """Approve or reject the invoice and record who decided."""
        decision = ApprovalDecision.model_validate(state["decision"])
        target = (
            InvoiceStatus.APPROVED
            if decision.verdict is Verdict.APPROVE
            else InvoiceStatus.REJECTED
        )
        invoice_id, now = invoice_id_of(state), deps.clock()
        with session_scope(deps.session_factory) as session:
            invoices = InvoiceRepository(session)
            invoice = invoices.get(invoice_id)
            if invoice is None:
                raise PermanentJobError(f"invoice {invoice_id} not found")
            if invoice.status is target:
                return {"status": target.value}  # replay: already applied and audited
            if invoice.status not in REVIEWABLE:
                raise StaleApprovalError(
                    f"invoice {invoice_id} is {invoice.status.value!r}, a decision cannot apply"
                )
            before = invoice.status
            invoices.save(invoice.transition_to(target, at=now), expected_status=before)
            payload: dict[str, JsonValue] = {
                "verdict": decision.verdict.value,
                "from": before.value,
                "to": target.value,
                "comment": decision.comment,
            }
            if state.get("trace_id"):
                payload["trace_id"] = state["trace_id"]
            redacted = redact_payload(payload)
            assert isinstance(redacted, dict)
            AuditEventRepository(session).append(
                AuditEvent(
                    id=uuid4(),
                    run_id=run_id_of(state),
                    step=STEP_APPROVAL,
                    actor_type=ActorType.HUMAN,
                    actor_id=decision.approver,
                    invoice_id=invoice_id,
                    payload=redacted,
                    occurred_at=now,
                )
            )
        return {"status": target.value}

    return apply_decision
