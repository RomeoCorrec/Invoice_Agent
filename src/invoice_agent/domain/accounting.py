"""Propose the accounting entry of a supplier invoice (French PCG).

Pure arithmetic on what was extracted and the supplier's default accounts: no model decides an
account. When the entry cannot be built soundly, the result says why instead of guessing, and
the invoice goes to a human with no entry to post.

    debit  charge account (one line per VAT rate)   net amount
    debit  44566 VAT deductible                       VAT
    credit supplier auxiliary account (401...)      gross amount
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.invoice import InvoiceData, VatBreakdownLine
from invoice_agent.domain.journal import JournalLine, Side
from invoice_agent.domain.supplier import Supplier
from invoice_agent.domain.types import NonEmptyStr, Siren

VAT_DEDUCTIBLE_ACCOUNT = "44566"
PURCHASES_JOURNAL = "ACH"
MAX_LABEL = 120
ZERO = Decimal("0.00")


class BlockReason(StrEnum):
    UNKNOWN_SUPPLIER = "unknown_supplier"
    FOREIGN_CURRENCY = "foreign_currency"
    NOT_A_PURCHASE = "not_a_purchase"
    """Zero or negative amounts: a credit note, which this version does not book."""
    TOTALS_MISMATCH = "totals_mismatch"
    BREAKDOWN_MISMATCH = "breakdown_mismatch"


class EntryProposal(DomainModel):
    """A balanced entry ready to be reviewed and, once approved, posted."""

    supplier_siren: Siren
    invoice_number: NonEmptyStr
    entry_date: date
    journal: NonEmptyStr = PURCHASES_JOURNAL
    lines: tuple[JournalLine, ...] = Field(min_length=2)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines if line.side is Side.CREDIT), start=ZERO)


class EntryBlocked(DomainModel):
    reason: BlockReason
    detail: NonEmptyStr


def propose_entry(data: InvoiceData, supplier: Supplier | None) -> EntryProposal | EntryBlocked:
    if supplier is None:
        return _blocked(BlockReason.UNKNOWN_SUPPLIER, "no known supplier for this invoice")
    if data.currency != "EUR":
        return _blocked(BlockReason.FOREIGN_CURRENCY, f"currency {data.currency} is not booked")
    if data.total_ht <= 0 or data.total_ttc <= 0 or data.total_vat < 0:
        return _blocked(BlockReason.NOT_A_PURCHASE, "amounts are zero or negative")
    if data.total_ht + data.total_vat != data.total_ttc:
        return _blocked(BlockReason.TOTALS_MISMATCH, "net plus VAT is not the gross total")

    basis = _basis_by_rate(data)
    if isinstance(basis, EntryBlocked):
        return basis

    label = f"Facture {data.invoice_number} - {supplier.name}"[:MAX_LABEL]
    lines = [
        JournalLine(
            account=supplier.default_expense_account,
            label=label if len(basis) == 1 else f"{label} (TVA {rate}%)"[:MAX_LABEL],
            side=Side.DEBIT,
            amount=amount,
        )
        for rate, amount in basis
    ]
    if data.total_vat > 0:
        lines.append(
            JournalLine(
                account=VAT_DEDUCTIBLE_ACCOUNT,
                label=f"TVA déductible - {label}"[:MAX_LABEL],
                side=Side.DEBIT,
                amount=data.total_vat,
            )
        )
    lines.append(
        JournalLine(
            account=supplier.supplier_account,
            label=label,
            side=Side.CREDIT,
            amount=data.total_ttc,
        )
    )
    return EntryProposal(
        supplier_siren=supplier.siren,
        invoice_number=data.invoice_number,
        entry_date=data.issue_date,
        lines=tuple(lines),
    )


def _basis_by_rate(data: InvoiceData) -> list[tuple[Decimal, Decimal]] | EntryBlocked:
    """Net amount per VAT rate, from the breakdown when the invoice has one."""
    breakdown: tuple[VatBreakdownLine, ...] = data.vat_breakdown
    if not breakdown:
        return [(ZERO, data.total_ht)]
    if any(line.basis <= 0 or line.vat < 0 for line in breakdown):
        return _blocked(BlockReason.BREAKDOWN_MISMATCH, "a VAT line has a non-positive basis")
    if (
        sum((line.basis for line in breakdown), start=ZERO) != data.total_ht
        or sum((line.vat for line in breakdown), start=ZERO) != data.total_vat
    ):
        return _blocked(BlockReason.BREAKDOWN_MISMATCH, "the VAT breakdown does not add up")
    return [(line.rate, line.basis) for line in breakdown]


def _blocked(reason: BlockReason, detail: str) -> EntryBlocked:
    return EntryBlocked(reason=reason, detail=detail)
