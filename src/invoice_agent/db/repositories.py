"""Repositories: the only code that talks SQL. They accept and return domain models.

They flush but never commit (see ``session``). Rows are not exposed.
"""

from collections.abc import Collection
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invoice_agent.db import mappers
from invoice_agent.db.models import (
    AuditEventRow,
    InvoiceRow,
    JournalEntryRow,
    PurchaseOrderRow,
    SupplierRow,
)
from invoice_agent.domain.audit import AuditEvent
from invoice_agent.domain.invoice import Invoice
from invoice_agent.domain.invoice_status import InvoiceStatus, check_transition
from invoice_agent.domain.journal import JournalEntry
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderStatus
from invoice_agent.domain.rules.duplicates import normalize_invoice_number
from invoice_agent.domain.supplier import Supplier

# Invoices that count against a purchase order: extracted and beyond, minus the dead ends.
BILLED_STATUSES = (
    InvoiceStatus.EXTRACTED,
    InvoiceStatus.VALIDATED,
    InvoiceStatus.NEEDS_REVIEW,
    InvoiceStatus.APPROVED,
    InvoiceStatus.POSTED,
)
# Copies that can make a new invoice a duplicate: every live copy (a copy still being
# extracted already has its source file hash), but not failed ones.
LIVE_STATUSES = (InvoiceStatus.RECEIVED, *BILLED_STATUSES, InvoiceStatus.REJECTED)
# Copies that have already been through the checks.
CHECKED_STATUSES = (
    InvoiceStatus.VALIDATED,
    InvoiceStatus.NEEDS_REVIEW,
    InvoiceStatus.APPROVED,
    InvoiceStatus.POSTED,
    InvoiceStatus.REJECTED,
)


class InvoiceNotFoundError(LookupError):
    pass


class StaleInvoiceError(RuntimeError):
    """The invoice is no longer in the status the caller based its decision on."""

    def __init__(self, invoice_id: UUID, expected: InvoiceStatus, actual: InvoiceStatus) -> None:
        super().__init__(
            f"invoice {invoice_id} is {actual.value!r}, expected {expected.value!r}: "
            "another worker changed it first"
        )
        self.invoice_id = invoice_id
        self.expected = expected
        self.actual = actual


class IdempotencyConflictError(RuntimeError):
    """An idempotency key is already used by an entry for a different invoice."""


class SupplierRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, supplier: Supplier) -> None:
        self._session.add(mappers.supplier_to_row(supplier))
        self._session.flush()

    def get(self, supplier_id: UUID) -> Supplier | None:
        row = self._session.get(SupplierRow, supplier_id)
        return mappers.supplier_to_domain(row) if row else None

    def get_by_code(self, code: str) -> Supplier | None:
        return self._first(SupplierRow.code == code)

    def get_by_siren(self, siren: str) -> Supplier | None:
        return self._first(SupplierRow.siren == siren)

    def get_by_vat_number(self, vat_number: str) -> Supplier | None:
        return self._first(SupplierRow.vat_number == vat_number)

    def _first(self, *conditions: ColumnElement[bool]) -> Supplier | None:
        row = self._session.scalars(select(SupplierRow).where(*conditions)).first()
        return mappers.supplier_to_domain(row) if row else None


class PurchaseOrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, order: PurchaseOrder) -> None:
        self._session.add(mappers.purchase_order_to_row(order))
        self._session.flush()

    def get(self, order_id: UUID) -> PurchaseOrder | None:
        row = self._session.get(PurchaseOrderRow, order_id)
        return mappers.purchase_order_to_domain(row) if row else None

    def get_by_number(self, number: str) -> PurchaseOrder | None:
        row = self._session.scalars(
            select(PurchaseOrderRow).where(PurchaseOrderRow.number == number)
        ).first()
        return mappers.purchase_order_to_domain(row) if row else None

    def list_open_for_supplier(self, supplier_id: UUID) -> list[PurchaseOrder]:
        rows = self._session.scalars(
            select(PurchaseOrderRow)
            .where(
                PurchaseOrderRow.supplier_id == supplier_id,
                PurchaseOrderRow.status == PurchaseOrderStatus.OPEN.value,
            )
            .order_by(PurchaseOrderRow.order_date, PurchaseOrderRow.number)
        )
        return [mappers.purchase_order_to_domain(row) for row in rows]


class InvoiceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, invoice: Invoice) -> None:
        self._session.add(mappers.invoice_to_row(invoice))
        self._session.flush()

    def get(self, invoice_id: UUID) -> Invoice | None:
        row = self._session.get(InvoiceRow, invoice_id)
        return mappers.invoice_to_domain(row) if row else None

    def save(self, invoice: Invoice, *, expected_status: InvoiceStatus) -> None:
        """Persist a modified invoice, provided it is still in ``expected_status``.

        The row is locked (``SELECT ... FOR UPDATE``) so two workers cannot both move
        the same invoice: the second one gets StaleInvoiceError instead of overwriting.
        The status change itself must be an allowed transition (or no change at all).
        """
        if invoice.status is not expected_status:
            check_transition(expected_status, invoice.status)
        row = self._session.get(InvoiceRow, invoice.id, with_for_update=True)
        if row is None:
            raise InvoiceNotFoundError(str(invoice.id))
        actual = InvoiceStatus(row.status)
        if actual is not expected_status:
            raise StaleInvoiceError(invoice.id, expected_status, actual)
        mappers.apply_invoice(row, invoice)
        self._session.flush()

    def find_by_supplier_and_number(self, supplier_id: UUID, invoice_number: str) -> list[Invoice]:
        """All invoices with this supplier and number, oldest first (duplicate detection)."""
        return self._many(
            InvoiceRow.supplier_id == supplier_id, InvoiceRow.invoice_number == invoice_number
        )

    def find_by_source_sha256(self, sha256: str) -> list[Invoice]:
        return self._many(InvoiceRow.source_sha256 == sha256)

    def list_by_status(self, status: InvoiceStatus) -> list[Invoice]:
        return self._many(InvoiceRow.status == status.value)

    def billed_ht_by_purchase_order(
        self, order_ids: Collection[UUID], *, excluding: UUID
    ) -> dict[UUID, Decimal]:
        """HT already invoiced per purchase order, by invoices other than ``excluding``."""
        if not order_ids:
            return {}
        rows = self._session.execute(
            select(InvoiceRow.purchase_order_id, func.sum(InvoiceRow.total_ht))
            .where(
                InvoiceRow.purchase_order_id.in_(order_ids),
                InvoiceRow.id != excluding,
                InvoiceRow.status.in_([status.value for status in BILLED_STATUSES]),
            )
            .group_by(InvoiceRow.purchase_order_id)
        )
        return {
            order_id: total
            for order_id, total in rows
            if order_id is not None and total is not None
        }

    def find_duplicates(self, invoice: Invoice, *, supplier_id: UUID | None) -> list[Invoice]:
        """Other invoices that look like the same document, in either direction.

        Same source file, or same invoice number (compared ignoring case and punctuation)
        from the same seller: the resolved supplier, or the same SIREN or VAT number
        written on the document, so a copy not yet linked to a supplier is still found.

        A copy received earlier always counts. A copy received later counts only once it
        has been through the checks: it could not have flagged this one. Together this
        guarantees that of two copies at least one is flagged, whatever the processing
        order. Ties on the reception time are broken by id.
        """
        earlier = or_(
            InvoiceRow.received_at < invoice.received_at,
            and_(InvoiceRow.received_at == invoice.received_at, InvoiceRow.id < invoice.id),
        )
        counts = or_(
            and_(earlier, InvoiceRow.status.in_([s.value for s in LIVE_STATUSES])),
            and_(~earlier, InvoiceRow.status.in_([s.value for s in CHECKED_STATUSES])),
        )
        same_document: list[ColumnElement[bool]] = [
            InvoiceRow.source_sha256 == invoice.source.sha256
        ]
        if invoice.data is not None:
            seller = invoice.data.seller
            same_seller: list[ColumnElement[bool]] = []
            if supplier_id is not None:
                same_seller.append(InvoiceRow.supplier_id == supplier_id)
            if seller.siren:
                same_seller.append(InvoiceRow.seller_siren == seller.siren)
            if seller.vat_number:
                same_seller.append(InvoiceRow.seller_vat_number == seller.vat_number)
            if same_seller:
                number = func.upper(
                    func.regexp_replace(InvoiceRow.invoice_number, "[^A-Za-z0-9]", "", "g")
                ) == normalize_invoice_number(invoice.data.invoice_number)
                same_document.append(and_(number, or_(*same_seller)))
        return self._many(InvoiceRow.id != invoice.id, counts, or_(*same_document))

    def _many(self, *conditions: ColumnElement[bool]) -> list[Invoice]:
        rows = self._session.scalars(
            select(InvoiceRow).where(*conditions).order_by(InvoiceRow.received_at, InvoiceRow.id)
        )
        return [mappers.invoice_to_domain(row) for row in rows]


class JournalEntryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entry: JournalEntry) -> JournalEntry:
        """Insert the entry, or return the existing one if the key was already used.

        Calling this twice with the same idempotency key never creates a second entry.
        """
        existing = self.get_by_idempotency_key(entry.idempotency_key)
        if existing is None:
            try:
                # Savepoint: losing a race must not poison the surrounding transaction.
                with self._session.begin_nested():
                    self._session.add(mappers.journal_entry_to_row(entry))
                    self._session.flush()
                return entry
            except IntegrityError:
                existing = self.get_by_idempotency_key(entry.idempotency_key)
                if existing is None:
                    raise
        if existing.invoice_id != entry.invoice_id:
            raise IdempotencyConflictError(
                f"key {entry.idempotency_key!r} already belongs to another invoice"
            )
        return existing

    def get_by_idempotency_key(self, key: str) -> JournalEntry | None:
        return self._one(JournalEntryRow.idempotency_key == key)

    def get_by_invoice(self, invoice_id: UUID) -> JournalEntry | None:
        return self._one(JournalEntryRow.invoice_id == invoice_id)

    def _one(self, condition: ColumnElement[bool]) -> JournalEntry | None:
        row = self._session.scalars(select(JournalEntryRow).where(condition)).first()
        return mappers.journal_entry_to_domain(row) if row else None


class AuditEventRepository:
    """Append and read only: there is deliberately no update or delete."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: AuditEvent) -> None:
        self._session.add(mappers.audit_event_to_row(event))
        self._session.flush()

    def list_for_invoice(self, invoice_id: UUID) -> list[AuditEvent]:
        return self._many(AuditEventRow.invoice_id == invoice_id)

    def list_for_run(self, run_id: UUID) -> list[AuditEvent]:
        return self._many(AuditEventRow.run_id == run_id)

    def _many(self, condition: ColumnElement[bool]) -> list[AuditEvent]:
        rows = self._session.scalars(
            select(AuditEventRow)
            .where(condition)
            .order_by(AuditEventRow.occurred_at, AuditEventRow.id)
        )
        return [mappers.audit_event_to_domain(row) for row in rows]
