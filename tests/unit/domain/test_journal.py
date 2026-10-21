from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.domain.journal import JournalEntry, JournalLine, Side


def line(account: str, side: Side, amount: str) -> JournalLine:
    return JournalLine(account=account, label=f"label {account}", side=side, amount=amount)


def make_entry(lines: tuple[JournalLine, ...]) -> JournalEntry:
    return JournalEntry(
        id=uuid4(),
        invoice_id=uuid4(),
        entry_date=date(2026, 9, 17),
        reference="CDA-26-0412",
        idempotency_key="invoice-1",
        lines=lines,
        created_at=datetime(2026, 9, 17, tzinfo=UTC),
    )


def inv05_lines() -> tuple[JournalLine, ...]:
    """Expected entry of INV-05 in the golden dataset."""
    return (
        line("6257", Side.DEBIT, "570.00"),
        line("44566", Side.DEBIT, "69.60"),
        line("401CAFE", Side.CREDIT, "639.60"),
    )


def test_balanced_entry_is_accepted() -> None:
    entry = make_entry(inv05_lines())
    assert entry.total_debit == entry.total_credit == Decimal("639.60")


def test_line_exposes_debit_and_credit_columns() -> None:
    debit = line("6257", Side.DEBIT, "570.00")
    credit = line("401CAFE", Side.CREDIT, "639.60")
    assert (debit.debit, debit.credit) == (Decimal("570.00"), Decimal("0.00"))
    assert (credit.debit, credit.credit) == (Decimal("0.00"), Decimal("639.60"))


def test_one_cent_off_is_rejected() -> None:
    lines = (*inv05_lines()[:2], line("401CAFE", Side.CREDIT, "639.59"))
    with pytest.raises(ValidationError, match="unbalanced"):
        make_entry(lines)


def test_only_debits_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unbalanced"):
        make_entry((line("6257", Side.DEBIT, "10.00"), line("44566", Side.DEBIT, "2.00")))


def test_needs_at_least_two_lines() -> None:
    with pytest.raises(ValidationError):
        make_entry((line("6257", Side.DEBIT, "10.00"),))


@pytest.mark.parametrize("amount", ["0.00", "-5.00"])
def test_line_amount_must_be_strictly_positive(amount: str) -> None:
    with pytest.raises(ValidationError):
        line("6257", Side.DEBIT, amount)


def test_line_rejects_explicit_debit_and_credit_fields() -> None:
    with pytest.raises(ValidationError):
        JournalLine(  # type: ignore[call-arg]
            account="6257", label="x", side=Side.DEBIT, amount="1.00", credit="1.00"
        )


def test_entry_cannot_be_unbalanced_after_construction() -> None:
    entry = make_entry(inv05_lines())
    with pytest.raises(ValidationError):
        entry.lines = (inv05_lines()[0],)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        entry.lines[0].amount = Decimal("1.00")  # type: ignore[misc]


def test_balance_is_exact_with_many_small_amounts() -> None:
    """0.10 + 0.20 would not equal 0.30 with floats; Decimal keeps it exact."""
    lines = (
        line("6064", Side.DEBIT, "0.10"),
        line("6064", Side.DEBIT, "0.20"),
        line("401MERC", Side.CREDIT, "0.30"),
    )
    assert make_entry(lines).total_debit == Decimal("0.30")
