"""What the ledger's tools accept and return.

These models are the contract between the MCP server and its clients, and the only thing the
two share. The entry lines reuse the agent's domain model, so an entry is validated the same
way on both sides.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoice_agent.domain.journal import JournalLine
from invoice_agent.domain.types import (
    AccountNumber,
    Money,
    NonEmptyStr,
    Quantity,
    Siren,
    Siret,
)

ZERO = Decimal("0.00")


class LedgerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorCode(StrEnum):
    """Why the ledger refused a request. It is the prefix of the error message."""

    UNKNOWN_SUPPLIER = "unknown_supplier"
    UNKNOWN_ACCOUNT = "unknown_account"
    UNBALANCED_ENTRY = "unbalanced_entry"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVOICE_ALREADY_BOOKED = "invoice_already_booked"
    INVALID_REQUEST = "invalid_request"


class LedgerRefusal(Exception):
    """A request the ledger understood and refused. The code is stable; the text is for people."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


class SupplierInfo(LedgerModel):
    code: NonEmptyStr
    name: NonEmptyStr
    siren: Siren
    siret: Siret
    vat_number: str | None = None
    iban_masked: str | None = None
    """Never the full IBAN: the ledger keeps only the masked form."""
    default_expense_account: AccountNumber
    supplier_account: AccountNumber


class PurchaseOrderLineInfo(LedgerModel):
    description: NonEmptyStr
    quantity: Quantity
    unit_price: Money
    amount_ht: Money


class PurchaseOrderInfo(LedgerModel):
    number: NonEmptyStr
    supplier_code: NonEmptyStr
    order_date: date
    status: str
    total_ht: Money
    lines: tuple[PurchaseOrderLineInfo, ...]


class AccountInfo(LedgerModel):
    number: AccountNumber
    label: NonEmptyStr


class BookedInvoice(LedgerModel):
    """An invoice the ledger already has an entry for."""

    entry_id: UUID
    supplier_siren: Siren
    invoice_number: NonEmptyStr
    entry_date: date
    total: Money


class EntryRequest(LedgerModel):
    """The only write the ledger accepts: one balanced entry for one supplier invoice."""

    idempotency_key: NonEmptyStr = Field(max_length=200)
    supplier_siren: Siren
    invoice_number: NonEmptyStr = Field(max_length=100)
    entry_date: date
    journal: NonEmptyStr = Field(default="ACH", max_length=16)
    lines: tuple[JournalLine, ...] = Field(min_length=2, max_length=50)

    @property
    def total_debit(self) -> Money:
        return sum((line.debit for line in self.lines), start=ZERO)

    @property
    def total_credit(self) -> Money:
        return sum((line.credit for line in self.lines), start=ZERO)

    @model_validator(mode="after")
    def _balanced(self) -> Self:
        if self.total_debit != self.total_credit:
            raise ValueError(
                f"{ErrorCode.UNBALANCED_ENTRY.value}: debit {self.total_debit} "
                f"!= credit {self.total_credit}"
            )
        return self


class PostedEntry(LedgerModel):
    entry_id: UUID
    idempotency_key: NonEmptyStr
    replayed: bool
    """True when the key had already been used with the same request: nothing new was written."""
    total: Money
