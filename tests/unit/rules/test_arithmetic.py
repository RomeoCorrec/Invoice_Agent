from decimal import Decimal

import pytest

from invoice_agent.domain.invoice import InvoiceLine, VatBreakdownLine
from invoice_agent.domain.rules.anomaly import AnomalyCode
from invoice_agent.domain.rules.arithmetic import check_totals, check_vat, expected_vat
from tests.factories import make_data


def line(amount: str, rate: str) -> InvoiceLine:
    return InvoiceLine(
        description="x", quantity="1", unit_price=amount, vat_rate=rate, amount_ht=amount
    )


def vat(rate: str, basis: str, amount: str) -> VatBreakdownLine:
    return VatBreakdownLine(rate=rate, basis=basis, vat=amount)


@pytest.mark.parametrize(
    ("basis", "rate", "expected"),
    [
        ("100.00", "20.00", "20.00"),
        ("880.00", "10.00", "88.00"),
        ("0.025", "20.00", "0.01"),  # 0.005 rounds half up
        ("0.02", "20.00", "0.00"),  # 0.004 rounds down
        ("33.33", "5.50", "1.83"),  # 1.83315
        ("10.00", "0.00", "0.00"),
    ],
)
def test_expected_vat_rounds_half_up(basis: str, rate: str, expected: str) -> None:
    assert expected_vat(Decimal(basis), Decimal(rate)) == Decimal(expected)


def test_consistent_invoice_has_no_arithmetic_anomaly() -> None:
    data = make_data()
    assert check_vat(data) == []
    assert check_totals(data) == []


def test_wrong_vat_amount_is_flagged_like_inv_12() -> None:
    data = make_data(
        lines=(line("880.00", "10.00"),),
        vat_breakdown=(vat("10.00", "880.00", "96.80"),),
        total_ht="880.00",
        total_vat="96.80",
        total_ttc="976.80",
    )
    [anomaly] = check_vat(data)
    assert anomaly.code is AnomalyCode.VAT_AMOUNT_MISMATCH
    assert "96.80" in anomaly.message
    assert "88.00" in anomaly.message
    assert check_totals(data) == []  # the totals are coherent with the wrong VAT


def test_ttc_not_equal_to_ht_plus_vat_is_flagged_like_inv_13() -> None:
    data = make_data(
        lines=(line("300.00", "20.00"),),
        vat_breakdown=(vat("20.00", "300.00", "60.00"),),
        total_ht="300.00",
        total_vat="60.00",
        total_ttc="390.00",
    )
    assert check_vat(data) == []
    [anomaly] = check_totals(data)
    assert anomaly.code is AnomalyCode.TOTAL_MISMATCH


def test_one_cent_difference_is_not_tolerated() -> None:
    data = make_data(total_ttc="639.61")
    assert [a.code for a in check_totals(data)] == [AnomalyCode.TOTAL_MISMATCH]


def test_rate_used_by_lines_but_missing_from_breakdown() -> None:
    data = make_data(vat_breakdown=make_data().vat_breakdown[:1])
    assert [a.code for a in check_vat(data)] == [AnomalyCode.VAT_AMOUNT_MISMATCH]


def test_total_ht_must_match_the_lines() -> None:
    [anomaly] = check_totals(make_data(total_ht="571.00", total_ttc="640.60"))
    assert "sum of lines" in anomaly.message


def test_breakdown_basis_must_match_the_lines_of_that_rate() -> None:
    data = make_data(
        vat_breakdown=(vat("20.00", "126.00", "25.20"), vat("10.00", "400.00", "40.00")),
        total_vat="65.20",
        total_ttc="635.20",
    )
    [anomaly] = check_totals(data)
    assert "rate 10.00%" in anomaly.message


def test_several_rounded_lines_use_the_declared_basis() -> None:
    """Rounding is applied once per rate on the basis, not line by line."""
    data = make_data(
        lines=(line("0.03", "20.00"), line("0.03", "20.00"), line("0.03", "20.00")),
        vat_breakdown=(vat("20.00", "0.09", "0.02"),),
        total_ht="0.09",
        total_vat="0.02",
        total_ttc="0.11",
    )
    assert check_vat(data) == []
    assert check_totals(data) == []


def test_exempt_lines_with_zero_vat() -> None:
    data = make_data(
        lines=(line("50.00", "0.00"),),
        vat_breakdown=(vat("0.00", "50.00", "0.00"),),
        total_ht="50.00",
        total_vat="0.00",
        total_ttc="50.00",
    )
    assert check_vat(data) == []
    assert check_totals(data) == []
