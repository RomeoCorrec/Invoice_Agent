"""SQLAlchemy tables.

These classes describe storage only. Business invariants live in ``domain/``; the
constraints here are a second line of defence (and the only one that holds against
direct SQL access). Conversion between the two layers is explicit, see ``mappers``.

Statuses and enums are stored as text with CHECK constraints rather than native
PostgreSQL enums: adding a value is a simple migration instead of ``ALTER TYPE``.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from invoice_agent.db.base import MONEY, QUANTITY, RATE, Base, in_values
from invoice_agent.domain.audit import ActorType
from invoice_agent.domain.invoice import SourceFormat
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.purchase_order import PurchaseOrderStatus

TIMESTAMP = DateTime(timezone=True)


class SupplierRow(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("(iban_masked IS NULL) = (iban_hash IS NULL)", name="iban_all_or_nothing"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Text)
    siren: Mapped[str] = mapped_column(String(9))
    siret: Mapped[str] = mapped_column(String(14))
    vat_number: Mapped[str | None] = mapped_column(String(32))
    iban_masked: Mapped[str | None] = mapped_column(String(32))
    iban_hash: Mapped[str | None] = mapped_column(String(64))
    default_expense_account: Mapped[str] = mapped_column(String(16))
    supplier_account: Mapped[str] = mapped_column(String(16))


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint(in_values("status", PurchaseOrderStatus), name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    number: Mapped[str] = mapped_column(String(64), unique=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    order_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(16))

    lines: Mapped[list["PurchaseOrderLineRow"]] = relationship(
        order_by="PurchaseOrderLineRow.line_no",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PurchaseOrderLineRow(Base):
    __tablename__ = "purchase_order_lines"

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), primary_key=True
    )
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    amount_ht: Mapped[Decimal] = mapped_column(MONEY)


_DATA_COLUMNS = (
    "issue_date",
    "currency",
    "seller_name",
    "total_ht",
    "total_vat",
    "total_ttc",
)
_REQUIRES_DATA = (
    InvoiceStatus.EXTRACTED,
    InvoiceStatus.VALIDATED,
    InvoiceStatus.NEEDS_REVIEW,
    InvoiceStatus.APPROVED,
    InvoiceStatus.REJECTED,
    InvoiceStatus.POSTED,
)


class InvoiceRow(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(in_values("status", InvoiceStatus), name="status_valid"),
        CheckConstraint(in_values("source_format", SourceFormat), name="source_format_valid"),
        # Extracted data is all present or all absent (invoice_number is the witness).
        CheckConstraint(
            " AND ".join(f"(invoice_number IS NULL) = ({col} IS NULL)" for col in _DATA_COLUMNS),
            name="data_all_or_nothing",
        ),
        CheckConstraint(
            "status NOT IN ("
            + ", ".join(f"'{s.value}'" for s in _REQUIRES_DATA)
            + ") OR invoice_number IS NOT NULL",
            name="data_required_for_status",
        ),
        CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)", name="failure_reason"
        ),
        CheckConstraint("updated_at >= received_at", name="updated_after_received"),
        CheckConstraint("(iban_masked IS NULL) = (iban_hash IS NULL)", name="iban_all_or_nothing"),
        # Deliberately NOT unique: a re-sent invoice (duplicate) must be storable to be flagged.
        Index("ix_invoices_supplier_id_invoice_number", "supplier_id", "invoice_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    source_uri: Mapped[str] = mapped_column(Text)
    source_format: Mapped[str] = mapped_column(String(16))
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_orders.id"), index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)

    # Extracted data (NULL until the invoice is extracted).
    invoice_number: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None]
    due_date: Mapped[date | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    seller_name: Mapped[str | None] = mapped_column(Text)
    seller_siren: Mapped[str | None] = mapped_column(String(9))
    seller_vat_number: Mapped[str | None] = mapped_column(String(32))
    buyer_name: Mapped[str | None] = mapped_column(Text)
    buyer_siren: Mapped[str | None] = mapped_column(String(9))
    buyer_vat_number: Mapped[str | None] = mapped_column(String(32))
    purchase_order_ref: Mapped[str | None] = mapped_column(Text)
    iban_masked: Mapped[str | None] = mapped_column(String(32))
    iban_hash: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    total_ht: Mapped[Decimal | None] = mapped_column(MONEY)
    total_vat: Mapped[Decimal | None] = mapped_column(MONEY)
    total_ttc: Mapped[Decimal | None] = mapped_column(MONEY)

    lines: Mapped[list["InvoiceLineRow"]] = relationship(
        order_by="InvoiceLineRow.line_no", cascade="all, delete-orphan", lazy="selectin"
    )
    vat_lines: Mapped[list["InvoiceVatLineRow"]] = relationship(
        order_by="InvoiceVatLineRow.line_no", cascade="all, delete-orphan", lazy="selectin"
    )


class InvoiceLineRow(Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), primary_key=True
    )
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    vat_rate: Mapped[Decimal] = mapped_column(RATE)
    amount_ht: Mapped[Decimal] = mapped_column(MONEY)


class InvoiceVatLineRow(Base):
    __tablename__ = "invoice_vat_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), primary_key=True
    )
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate: Mapped[Decimal] = mapped_column(RATE)
    basis: Mapped[Decimal] = mapped_column(MONEY)
    vat: Mapped[Decimal] = mapped_column(MONEY)


class JournalEntryRow(Base):
    """Immutable and balanced; both properties are enforced by triggers (see migration)."""

    __tablename__ = "journal_entries"
    __table_args__ = (UniqueConstraint("invoice_id", name="uq_journal_entries_invoice_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"))
    journal: Mapped[str] = mapped_column(String(8))
    entry_date: Mapped[date]
    reference: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)

    lines: Mapped[list["JournalLineRow"]] = relationship(
        order_by="JournalLineRow.line_no", lazy="selectin"
    )


class JournalLineRow(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_non_negative"),
        # Exactly one side carries the amount.
        CheckConstraint("(debit > 0) <> (credit > 0)", name="one_side_only"),
    )

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journal_entries.id"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(Text)
    debit: Mapped[Decimal] = mapped_column(MONEY)
    credit: Mapped[Decimal] = mapped_column(MONEY)


class AuditEventRow(Base):
    """Append-only; UPDATE and DELETE are rejected by a trigger (see migration)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(in_values("actor_type", ActorType), name="actor_type_valid"),
        Index("ix_audit_events_run_id", "run_id"),
        Index("ix_audit_events_invoice_id_occurred_at", "invoice_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    """Insertion order: events of one step can share a timestamp, ids are random."""
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(Text)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP)
