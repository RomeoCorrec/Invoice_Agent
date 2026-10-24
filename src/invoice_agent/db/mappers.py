"""Explicit conversion between domain models and table rows.

No automatic ORM <-> Pydantic magic: every field is mapped by hand so a schema change
shows up here, in review, and in the round-trip tests.
"""

from invoice_agent.db.models import (
    AuditEventRow,
    InvoiceLineRow,
    InvoiceRow,
    InvoiceVatLineRow,
    JobRow,
    JournalEntryRow,
    JournalLineRow,
    PurchaseOrderLineRow,
    PurchaseOrderRow,
    SupplierRow,
)
from invoice_agent.domain.audit import ActorType, AuditEvent
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import (
    Invoice,
    InvoiceData,
    InvoiceLine,
    Party,
    SourceDocument,
    SourceFormat,
    VatBreakdownLine,
)
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import Job, JobStatus
from invoice_agent.domain.journal import JournalEntry, JournalLine, Side
from invoice_agent.domain.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from invoice_agent.domain.supplier import Supplier

# --- supplier ----------------------------------------------------------------------------


def supplier_to_row(supplier: Supplier) -> SupplierRow:
    return SupplierRow(
        id=supplier.id,
        code=supplier.code,
        name=supplier.name,
        siren=supplier.siren,
        siret=supplier.siret,
        vat_number=supplier.vat_number,
        iban_masked=supplier.iban.masked if supplier.iban else None,
        iban_hash=supplier.iban.digest if supplier.iban else None,
        default_expense_account=supplier.default_expense_account,
        supplier_account=supplier.supplier_account,
    )


def supplier_to_domain(row: SupplierRow) -> Supplier:
    return Supplier(
        id=row.id,
        code=row.code,
        name=row.name,
        siren=row.siren,
        siret=row.siret,
        vat_number=row.vat_number,
        iban=_fingerprint(row.iban_masked, row.iban_hash),
        default_expense_account=row.default_expense_account,
        supplier_account=row.supplier_account,
    )


def _fingerprint(masked: str | None, digest: str | None) -> IbanFingerprint | None:
    if masked is None or digest is None:
        return None
    return IbanFingerprint(masked=masked, digest=digest)


# --- purchase order ----------------------------------------------------------------------


def purchase_order_to_row(order: PurchaseOrder) -> PurchaseOrderRow:
    return PurchaseOrderRow(
        id=order.id,
        number=order.number,
        supplier_id=order.supplier_id,
        order_date=order.order_date,
        status=order.status.value,
        lines=[
            PurchaseOrderLineRow(
                line_no=number,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount_ht=line.amount_ht,
            )
            for number, line in enumerate(order.lines, start=1)
        ],
    )


def purchase_order_to_domain(row: PurchaseOrderRow) -> PurchaseOrder:
    return PurchaseOrder(
        id=row.id,
        number=row.number,
        supplier_id=row.supplier_id,
        order_date=row.order_date,
        status=PurchaseOrderStatus(row.status),
        lines=tuple(
            PurchaseOrderLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount_ht=line.amount_ht,
            )
            for line in row.lines
        ),
    )


# --- invoice -----------------------------------------------------------------------------


def invoice_to_row(invoice: Invoice) -> InvoiceRow:
    row = InvoiceRow(id=invoice.id)
    apply_invoice(row, invoice)
    return row


def apply_invoice(row: InvoiceRow, invoice: Invoice) -> None:
    """Copy every field of ``invoice`` onto ``row`` (insert or update)."""
    row.status = invoice.status.value
    row.source_uri = invoice.source.uri
    row.source_format = invoice.source.format.value
    row.source_sha256 = invoice.source.sha256
    row.supplier_id = invoice.supplier_id
    row.purchase_order_id = invoice.purchase_order_id
    row.failure_reason = invoice.failure_reason
    row.received_at = invoice.received_at
    row.updated_at = invoice.updated_at

    data = invoice.data
    seller, buyer = (data.seller, data.buyer) if data else (None, None)
    row.invoice_number = data.invoice_number if data else None
    row.issue_date = data.issue_date if data else None
    row.due_date = data.due_date if data else None
    row.currency = data.currency if data else None
    row.seller_name = seller.name if seller else None
    row.seller_siren = seller.siren if seller else None
    row.seller_vat_number = seller.vat_number if seller else None
    row.buyer_name = buyer.name if buyer else None
    row.buyer_siren = buyer.siren if buyer else None
    row.buyer_vat_number = buyer.vat_number if buyer else None
    row.purchase_order_ref = data.purchase_order_ref if data else None
    row.iban_masked = data.iban.masked if data and data.iban else None
    row.iban_hash = data.iban.digest if data and data.iban else None
    row.notes = list(data.notes) if data else []
    row.total_ht = data.total_ht if data else None
    row.total_vat = data.total_vat if data else None
    row.total_ttc = data.total_ttc if data else None

    row.lines = [
        InvoiceLineRow(
            line_no=number,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
            vat_rate=line.vat_rate,
            amount_ht=line.amount_ht,
        )
        for number, line in enumerate(data.lines if data else (), start=1)
    ]
    row.vat_lines = [
        InvoiceVatLineRow(line_no=number, rate=line.rate, basis=line.basis, vat=line.vat)
        for number, line in enumerate(data.vat_breakdown if data else (), start=1)
    ]


def invoice_to_domain(row: InvoiceRow) -> Invoice:
    return Invoice(
        id=row.id,
        status=InvoiceStatus(row.status),
        source=SourceDocument(
            uri=row.source_uri,
            format=SourceFormat(row.source_format),
            sha256=row.source_sha256,
        ),
        data=_invoice_data(row),
        supplier_id=row.supplier_id,
        purchase_order_id=row.purchase_order_id,
        failure_reason=row.failure_reason,
        received_at=row.received_at,
        updated_at=row.updated_at,
    )


def _invoice_data(row: InvoiceRow) -> InvoiceData | None:
    # The data_all_or_nothing CHECK guarantees these are all set when the number is.
    if (
        row.invoice_number is None
        or row.issue_date is None
        or row.seller_name is None
        or row.total_ht is None
        or row.total_vat is None
        or row.total_ttc is None
    ):
        return None
    buyer = (
        Party(name=row.buyer_name, siren=row.buyer_siren, vat_number=row.buyer_vat_number)
        if row.buyer_name
        else None
    )
    return InvoiceData(
        invoice_number=row.invoice_number,
        issue_date=row.issue_date,
        due_date=row.due_date,
        currency=row.currency or "EUR",
        seller=Party(
            name=row.seller_name, siren=row.seller_siren, vat_number=row.seller_vat_number
        ),
        buyer=buyer,
        purchase_order_ref=row.purchase_order_ref,
        iban=_fingerprint(row.iban_masked, row.iban_hash),
        notes=tuple(row.notes),
        lines=tuple(
            InvoiceLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_rate=line.vat_rate,
                amount_ht=line.amount_ht,
            )
            for line in row.lines
        ),
        vat_breakdown=tuple(
            VatBreakdownLine(rate=line.rate, basis=line.basis, vat=line.vat)
            for line in row.vat_lines
        ),
        total_ht=row.total_ht,
        total_vat=row.total_vat,
        total_ttc=row.total_ttc,
    )


# --- journal entry -----------------------------------------------------------------------


def journal_entry_to_row(entry: JournalEntry) -> JournalEntryRow:
    return JournalEntryRow(
        id=entry.id,
        invoice_id=entry.invoice_id,
        journal=entry.journal,
        entry_date=entry.entry_date,
        reference=entry.reference,
        idempotency_key=entry.idempotency_key,
        created_at=entry.created_at,
        lines=[
            JournalLineRow(
                line_no=number,
                account=line.account,
                label=line.label,
                debit=line.debit,
                credit=line.credit,
            )
            for number, line in enumerate(entry.lines, start=1)
        ],
    )


def journal_entry_to_domain(row: JournalEntryRow) -> JournalEntry:
    return JournalEntry(
        id=row.id,
        invoice_id=row.invoice_id,
        journal=row.journal,
        entry_date=row.entry_date,
        reference=row.reference,
        idempotency_key=row.idempotency_key,
        lines=tuple(
            JournalLine(
                account=line.account,
                label=line.label,
                side=Side.DEBIT if line.debit > 0 else Side.CREDIT,
                amount=line.debit if line.debit > 0 else line.credit,
            )
            for line in row.lines
        ),
        created_at=row.created_at,
    )


# --- audit event -------------------------------------------------------------------------


def audit_event_to_row(event: AuditEvent) -> AuditEventRow:
    return AuditEventRow(
        id=event.id,
        run_id=event.run_id,
        step=event.step,
        actor_type=event.actor_type.value,
        actor_id=event.actor_id,
        invoice_id=event.invoice_id,
        payload=event.payload,
        occurred_at=event.occurred_at,
    )


def audit_event_to_domain(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        run_id=row.run_id,
        step=row.step,
        actor_type=ActorType(row.actor_type),
        actor_id=row.actor_id,
        invoice_id=row.invoice_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )


# --- job ---------------------------------------------------------------------------------


def job_to_row(job: Job) -> JobRow:
    row = JobRow(id=job.id)
    apply_job(row, job)
    return row


def apply_job(row: JobRow, job: Job) -> None:
    row.kind = job.kind
    row.payload = dict(job.payload)
    row.status = job.status.value
    row.attempts = job.attempts
    row.max_attempts = job.max_attempts
    row.run_after = job.run_after
    row.locked_by = job.locked_by
    row.locked_until = job.locked_until
    row.last_error = job.last_error
    row.idempotency_key = job.idempotency_key
    row.created_at = job.created_at
    row.updated_at = job.updated_at


def job_to_domain(row: JobRow) -> Job:
    return Job(
        id=row.id,
        kind=row.kind,
        payload=row.payload,
        status=JobStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        run_after=row.run_after,
        locked_by=row.locked_by,
        locked_until=row.locked_until,
        last_error=row.last_error,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
