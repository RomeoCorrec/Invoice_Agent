from decimal import Decimal

import pytest

from invoice_agent.domain.accounting import (
    BlockReason,
    EntryBlocked,
    EntryProposal,
    propose_entry,
)
from invoice_agent.domain.invoice import InvoiceData, VatBreakdownLine
from invoice_agent.domain.journal import Side
from tests.factories import GROUND_TRUTH, golden_extraction, make_supplier


def case(name: str) -> InvoiceData:
    return golden_extraction(GROUND_TRUTH / f"{name}.json")


def proposal(data: InvoiceData) -> EntryProposal:
    result = propose_entry(data, make_supplier())
    assert isinstance(result, EntryProposal), result
    return result


def blocked(data: InvoiceData, supplier: object = ...) -> EntryBlocked:
    result = propose_entry(data, make_supplier() if supplier is ... else supplier)  # type: ignore[arg-type]
    assert isinstance(result, EntryBlocked), result
    return result


def test_one_rate_gives_a_charge_a_vat_and_a_supplier_line() -> None:
    entry = proposal(case("INV-01"))
    assert [(line.account, line.side, line.amount) for line in entry.lines] == [
        ("6064", Side.DEBIT, Decimal("300.00")),
        ("44566", Side.DEBIT, Decimal("60.00")),
        ("401MERC", Side.CREDIT, Decimal("360.00")),
    ]
    assert entry.total == Decimal("360.00")
    assert entry.journal == "ACH" and entry.invoice_number == "PM-2026-0457"
    assert entry.entry_date == case("INV-01").issue_date
    assert entry.supplier_siren == "151757747"


def test_two_rates_give_one_charge_line_per_rate_and_one_vat_line() -> None:
    entry = proposal(case("INV-05"))
    charges = [line for line in entry.lines if line.account == "6064"]
    assert sorted(line.amount for line in charges) == sorted(
        item.basis for item in case("INV-05").vat_breakdown
    )
    assert all("TVA" in line.label for line in charges)
    [vat] = [line for line in entry.lines if line.account == "44566"]
    assert vat.amount == Decimal("69.60")
    assert sum(line.debit for line in entry.lines) == sum(line.credit for line in entry.lines)


def test_every_golden_invoice_with_coherent_totals_gets_a_balanced_entry() -> None:
    for path in sorted(GROUND_TRUTH.glob("INV-*.json")):
        data = golden_extraction(path)
        result = propose_entry(data, make_supplier())
        if data.total_ht + data.total_vat != data.total_ttc:
            assert isinstance(result, EntryBlocked) and result.reason is BlockReason.TOTALS_MISMATCH
        else:
            assert isinstance(result, EntryProposal), path.stem
            assert sum(l.debit for l in result.lines) == data.total_ttc  # noqa: E741


def test_an_invoice_without_a_vat_line_is_booked_on_the_net_total() -> None:
    data = case("INV-01").model_copy(update={"vat_breakdown": ()})
    entry = proposal(data)
    assert [line.account for line in entry.lines] == ["6064", "44566", "401MERC"]
    assert entry.lines[0].amount == Decimal("300.00")


def test_an_exempt_invoice_has_no_vat_line() -> None:
    data = case("INV-01").model_copy(
        update={
            "total_vat": Decimal("0.00"),
            "total_ttc": Decimal("300.00"),
            "vat_breakdown": (
                VatBreakdownLine(rate=Decimal("0"), basis=Decimal("300.00"), vat=Decimal("0.00")),
            ),
        }
    )
    entry = proposal(data)
    assert [line.account for line in entry.lines] == ["6064", "401MERC"]


def test_the_entry_uses_the_suppliers_own_accounts() -> None:
    supplier = make_supplier(default_expense_account="6156", supplier_account="401CLIM")
    result = propose_entry(case("INV-01"), supplier)
    assert isinstance(result, EntryProposal)
    assert {line.account for line in result.lines} == {"6156", "44566", "401CLIM"}


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"currency": "USD"}, BlockReason.FOREIGN_CURRENCY),
        (
            {"total_ht": Decimal("-300.00"), "total_ttc": Decimal("-240.00")},
            BlockReason.NOT_A_PURCHASE,
        ),
        ({"total_ttc": Decimal("390.00")}, BlockReason.TOTALS_MISMATCH),
        (
            {
                "vat_breakdown": (
                    VatBreakdownLine(
                        rate=Decimal("20"), basis=Decimal("250.00"), vat=Decimal("60.00")
                    ),
                )
            },
            BlockReason.BREAKDOWN_MISMATCH,
        ),
    ],
)
def test_an_entry_that_cannot_be_built_soundly_says_why(
    update: dict[str, object], reason: BlockReason
) -> None:
    assert blocked(case("INV-01").model_copy(update=update)).reason is reason


def test_an_unknown_supplier_gets_no_entry_and_no_guessed_account() -> None:
    assert blocked(case("INV-01"), None).reason is BlockReason.UNKNOWN_SUPPLIER


def test_labels_stay_within_the_ledger_limit() -> None:
    supplier = make_supplier(name="N" * 500)
    result = propose_entry(case("INV-01"), supplier)
    assert isinstance(result, EntryProposal)
    assert all(len(line.label) <= 120 for line in result.lines)
