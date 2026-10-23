"""Validation service: load the context, run the checks, record the outcome.

This is the unit a LangGraph node will call. Everything it does happens in the caller's
transaction: the invoice change and its audit event are committed (or rolled back) together.
"""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy.orm import Session

from invoice_agent.agent.checks import ValidationContext, ValidationResult, run_checks
from invoice_agent.db.repositories import (
    AuditEventRepository,
    InvoiceNotFoundError,
    InvoiceRepository,
    PurchaseOrderRepository,
    SupplierRepository,
)
from invoice_agent.domain.audit import ActorType, AuditEvent
from invoice_agent.domain.iban import redact_ibans
from invoice_agent.domain.invoice import Invoice, InvoiceData
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.supplier import Supplier
from invoice_agent.matching.supplier import resolve_supplier

ACTOR_ID = "rules-engine"


class NotReadyForValidationError(ValueError):
    pass


def validate_invoice(
    session: Session, invoice_id: UUID, *, run_id: UUID, now: datetime
) -> ValidationResult:
    """Check an extracted invoice and move it to validated or needs_review.

    The invoice is also linked to its supplier and purchase order when they are found.
    Raises NotReadyForValidationError if the invoice is not in the extracted status.
    """
    invoices = InvoiceRepository(session)
    invoice = invoices.get(invoice_id)
    if invoice is None:
        raise InvoiceNotFoundError(str(invoice_id))
    if invoice.status is not InvoiceStatus.EXTRACTED or invoice.data is None:
        raise NotReadyForValidationError(
            f"invoice {invoice_id} is {invoice.status.value!r}, expected 'extracted'"
        )

    context = _load_context(session, invoice, invoice.data)
    result = run_checks(invoice.data, context)

    updated = invoice.with_links(
        supplier_id=result.supplier_id, purchase_order_id=result.purchase_order_id, at=now
    ).transition_to(result.status, at=now)
    invoices.save(updated, expected_status=InvoiceStatus.EXTRACTED)

    AuditEventRepository(session).append(
        AuditEvent(
            id=uuid4(),
            run_id=run_id,
            step="validate",
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            invoice_id=invoice_id,
            payload=_audit_payload(result),
            occurred_at=now,
        )
    )
    return result


def _load_context(session: Session, invoice: Invoice, data: InvoiceData) -> ValidationContext:
    suppliers = SupplierRepository(session)
    orders = PurchaseOrderRepository(session)
    invoices = InvoiceRepository(session)

    candidates: list[Supplier] = []
    for found in (
        suppliers.get_by_siren(data.seller.siren) if data.seller.siren else None,
        suppliers.get_by_vat_number(data.seller.vat_number) if data.seller.vat_number else None,
    ):
        if found is not None and found not in candidates:
            candidates.append(found)
    supplier = resolve_supplier(data.seller, candidates)

    referenced = orders.get_by_number(data.purchase_order_ref) if data.purchase_order_ref else None
    open_orders = orders.list_open_for_supplier(supplier.id) if supplier else []
    order_ids = {order.id for order in open_orders}
    if referenced is not None:
        order_ids.add(referenced.id)

    return ValidationContext(
        supplier_candidates=tuple(candidates),
        referenced_order=referenced,
        open_orders=tuple(open_orders),
        billed_ht=invoices.billed_ht_by_purchase_order(order_ids, excluding=invoice.id),
        earlier_duplicates=tuple(
            invoices.find_earlier_duplicates(invoice, supplier_id=supplier.id if supplier else None)
        ),
    )


def _audit_payload(result: ValidationResult) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": result.status.value,
        "supplier_id": str(result.supplier_id) if result.supplier_id else None,
        "purchase_order_id": str(result.purchase_order_id) if result.purchase_order_id else None,
        "anomalies": [anomaly.model_dump(mode="json") for anomaly in result.anomalies],
    }
    return _redact(payload)  # type: ignore[return-value]


def _redact(value: JsonValue) -> JsonValue:
    """Invoice text can end up in messages: never let a full IBAN reach the audit log."""
    if isinstance(value, str):
        return redact_ibans(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value
