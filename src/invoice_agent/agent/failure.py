"""What becomes of an invoice when the job that was processing it gives up."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from invoice_agent.agent.graph.nodes import STEP_RECEIVE
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.invoice_status import TERMINAL_STATUSES, InvoiceStatus

ACTOR_ID = "worker"
MAX_REASON_LENGTH = 300
STEP_FAIL = "fail"


def fail_invoice_of_run(
    session: Session, run_id: UUID, *, now: datetime, reason: str
) -> UUID | None:
    """Move the invoice a run was processing to ``failed``, and audit why.

    Returns the invoice id, or ``None`` when there is nothing to do: the run died before
    it stored an invoice, or the invoice already reached a final status.
    """
    events = AuditEventRepository(session)
    invoice_id = next(
        (e.invoice_id for e in events.list_for_run(run_id) if e.step == STEP_RECEIVE), None
    )
    invoices = InvoiceRepository(session)
    invoice = invoices.get(invoice_id) if invoice_id else None
    if invoice is None or invoice.status in TERMINAL_STATUSES:
        return None
    short = reason[:MAX_REASON_LENGTH]
    invoices.save(
        invoice.transition_to(InvoiceStatus.FAILED, at=now, failure_reason=short),
        expected_status=invoice.status,
    )
    payload = redact_payload({"reason": short, "from": invoice.status.value})
    assert isinstance(payload, dict)
    events.append(
        AuditEvent(
            id=uuid4(),
            run_id=run_id,
            step=STEP_FAIL,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            invoice_id=invoice.id,
            payload=payload,
            occurred_at=now,
        )
    )
    return invoice.id
