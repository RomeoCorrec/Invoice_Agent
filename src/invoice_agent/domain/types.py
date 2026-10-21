"""Constrained scalar types used across the domain.

Money and quantities are ``Decimal`` with a fixed number of decimals. Values with more
decimals are rejected rather than rounded, and floats are refused outright.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, Field, StringConstraints


def _fixed_point(places: int) -> Callable[[object], Decimal]:
    quantum = Decimal(1).scaleb(-places)

    def validate(value: object) -> Decimal:
        if isinstance(value, float | bool):
            raise ValueError("amounts must be Decimal, str or int, never float")
        if not isinstance(value, Decimal | str | int):
            raise ValueError("amounts must be Decimal, str or int")
        try:
            number = Decimal(value)
            if not number.is_finite() or number != number.quantize(quantum):
                raise ValueError(f"at most {places} decimal places allowed")
            return number.quantize(quantum)
        except InvalidOperation as exc:
            raise ValueError("not a valid decimal number") from exc

    return validate


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


Money = Annotated[Decimal, BeforeValidator(_fixed_point(2)), Field(max_digits=12, decimal_places=2)]
"""Signed amount in euros, 2 decimals (credit notes can be negative)."""

PositiveMoney = Annotated[Money, Field(gt=0)]

Quantity = Annotated[
    Decimal, BeforeValidator(_fixed_point(4)), Field(max_digits=12, decimal_places=4)
]

VatRate = Annotated[
    Decimal, BeforeValidator(_fixed_point(2)), Field(max_digits=5, decimal_places=2, ge=0, le=100)
]
"""VAT rate in percent, e.g. ``Decimal("20.00")``."""

Siren = Annotated[str, StringConstraints(pattern=r"^\d{9}$")]
Siret = Annotated[str, StringConstraints(pattern=r"^\d{14}$")]
"""Format only. The Luhn check is a business rule (step 2), so invalid ones stay representable."""

AccountNumber = Annotated[str, StringConstraints(pattern=r"^\d{2,6}[A-Z0-9]{0,8}$")]
"""PCG account, possibly with an auxiliary suffix such as ``401MERC``."""

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""Timezone-aware datetime normalised to UTC."""
