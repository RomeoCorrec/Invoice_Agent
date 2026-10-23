import pytest

from invoice_agent.domain.rules.duplicates import normalize_invoice_number


@pytest.mark.parametrize(
    "variant", ["PM-2026-0457", "pm-2026-0457", " PM 2026 0457 ", "PM/2026.0457"]
)
def test_variants_of_the_same_number_are_equal(variant: str) -> None:
    assert normalize_invoice_number(variant) == "PM20260457"


def test_different_numbers_stay_different() -> None:
    assert normalize_invoice_number("PM-2026-0457") != normalize_invoice_number("PM-2026-0458")
