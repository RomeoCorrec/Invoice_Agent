"""Accounting entries (journal entries) in the French PCG.

Design: a line has a ``side`` and a strictly positive ``amount`` rather than two
``debit``/``credit`` columns, so "both set" and "both zero" cannot be represented.
``JournalEntry`` is immutable and its validator enforces double-entry bookkeeping
(total debits == total credits), so any existing entry is balanced by construction.
The database repeats this check with a deferred constraint trigger (defence in depth).
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.types import AccountNumber, Money, NonEmptyStr, PositiveMoney, UtcDatetime

ZERO = Decimal("0.00")


class Side(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalLine(DomainModel):
    account: AccountNumber
    label: NonEmptyStr
    side: Side
    amount: PositiveMoney

    @property
    def debit(self) -> Money:
        return self.amount if self.side is Side.DEBIT else ZERO

    @property
    def credit(self) -> Money:
        return self.amount if self.side is Side.CREDIT else ZERO


class JournalEntry(DomainModel):
    id: UUID
    invoice_id: UUID
    journal: NonEmptyStr = "ACH"
    entry_date: date
    reference: NonEmptyStr
    idempotency_key: NonEmptyStr
    """Posting twice with the same key must not create a second entry."""
    lines: tuple[JournalLine, ...] = Field(min_length=2)
    created_at: UtcDatetime

    @property
    def total_debit(self) -> Money:
        return sum((line.debit for line in self.lines), start=ZERO)

    @property
    def total_credit(self) -> Money:
        return sum((line.credit for line in self.lines), start=ZERO)

    @model_validator(mode="after")
    def _check_balanced(self) -> Self:
        if self.total_debit != self.total_credit:
            raise ValueError(
                f"unbalanced entry: debit {self.total_debit} != credit {self.total_credit}"
            )
        return self
