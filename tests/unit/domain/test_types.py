from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from invoice_agent.domain.types import (
    AccountNumber,
    Money,
    PositiveMoney,
    Quantity,
    Siren,
    UtcDatetime,
    VatRate,
)


class Sample(BaseModel):
    money: Money = Decimal("0.00")
    positive: PositiveMoney = Decimal("1.00")
    quantity: Quantity = Decimal("1")
    rate: VatRate = Decimal("20.00")
    siren: Siren = "123456789"
    account: AccountNumber = "6064"
    at: UtcDatetime = datetime(2026, 1, 1, tzinfo=UTC)


def test_money_is_quantized_to_two_decimals() -> None:
    assert str(Sample(money="570").money) == "570.00"
    assert str(Sample(money=Decimal("12.5")).money) == "12.50"
    assert str(Sample(money="12.500").money) == "12.50"


def test_money_accepts_negative_amounts_for_credit_notes() -> None:
    assert Sample(money="-10.00").money == Decimal("-10.00")


@pytest.mark.parametrize("bad", [12.5, True, "12.505", "abc", "NaN", "Infinity", None, [1]])
def test_money_rejects_floats_and_extra_precision(bad: object) -> None:
    with pytest.raises(ValidationError):
        Sample(money=bad)  # type: ignore[arg-type]


def test_money_rejects_more_than_twelve_digits() -> None:
    with pytest.raises(ValidationError):
        Sample(money="10000000000.00")


@pytest.mark.parametrize("bad", ["0", "-1.00"])
def test_positive_money_rejects_zero_and_negative(bad: str) -> None:
    with pytest.raises(ValidationError):
        Sample(positive=bad)


def test_quantity_allows_four_decimals() -> None:
    assert Sample(quantity="2.5").quantity == Decimal("2.5000")
    with pytest.raises(ValidationError):
        Sample(quantity="1.00001")


@pytest.mark.parametrize("bad", ["-0.01", "100.01"])
def test_vat_rate_is_a_percentage(bad: str) -> None:
    with pytest.raises(ValidationError):
        Sample(rate=bad)


@pytest.mark.parametrize("bad", ["12345678", "1234567890", "12345678a", ""])
def test_siren_is_nine_digits(bad: str) -> None:
    with pytest.raises(ValidationError):
        Sample(siren=bad)


@pytest.mark.parametrize("good", ["6064", "44566", "401MERC"])
def test_account_number_accepts_pcg_accounts(good: str) -> None:
    assert Sample(account=good).account == good


@pytest.mark.parametrize("bad", ["6", "ABC", "401merc", "40 1"])
def test_account_number_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValidationError):
        Sample(account=bad)


def test_datetime_is_normalized_to_utc() -> None:
    paris = timezone(timedelta(hours=2))
    value = Sample(at=datetime(2026, 9, 1, 12, 0, tzinfo=paris)).at
    assert value == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Sample(at=datetime(2026, 9, 1, 12, 0))
