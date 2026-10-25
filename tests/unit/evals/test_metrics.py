from decimal import Decimal

import pytest

from evals.metrics import Confusion, accuracy, compare_extraction, confusion, percentile
from tests.factories import GROUND_TRUTH, golden_extraction

TRUTH = golden_extraction(GROUND_TRUTH / "INV-05.json")


def test_a_perfect_extraction_has_every_field_right() -> None:
    fields = compare_extraction(TRUTH, TRUTH)
    assert all(fields.values())
    assert accuracy(fields) == 1.0
    assert {
        "invoice_number",
        "total_ttc",
        "seller.siren",
        "lines[2].amount_ht",
        "vat_breakdown",
    } <= set(fields)


def test_a_missing_extraction_gets_every_field_wrong() -> None:
    fields = compare_extraction(TRUTH, None)
    assert not any(fields.values())
    assert accuracy(fields) == 0.0


def test_one_wrong_amount_costs_exactly_one_field() -> None:
    wrong = TRUTH.model_copy(update={"total_ttc": Decimal("639.61")})
    fields = compare_extraction(TRUTH, wrong)
    assert [name for name, ok in fields.items() if not ok] == ["total_ttc"]
    assert accuracy(fields) == pytest.approx(1 - 1 / len(fields))


def test_amounts_must_be_exactly_equal() -> None:
    assert (
        compare_extraction(TRUTH, TRUTH.model_copy(update={"total_ht": Decimal("570.01")}))[
            "total_ht"
        ]
        is False
    )


def test_text_ignores_whitespace_but_nothing_else() -> None:
    seller = TRUTH.seller.model_copy(update={"name": "  Café   des Ateliers  SARL "})
    assert compare_extraction(TRUTH, TRUTH.model_copy(update={"seller": seller}))["seller.name"]
    other = TRUTH.seller.model_copy(update={"name": "Café des Atelier SARL"})
    assert not compare_extraction(TRUTH, TRUTH.model_copy(update={"seller": other}))["seller.name"]


def test_a_missing_line_fails_the_count_and_its_fields() -> None:
    fields = compare_extraction(TRUTH, TRUTH.model_copy(update={"lines": TRUTH.lines[:1]}))
    assert fields["lines.count"] is False
    assert fields["lines[1].description"] is True
    assert fields["lines[2].description"] is False


def test_vat_breakdown_is_compared_as_a_set() -> None:
    reversed_ = TRUTH.model_copy(update={"vat_breakdown": tuple(reversed(TRUTH.vat_breakdown))})
    assert compare_extraction(TRUTH, reversed_)["vat_breakdown"]


def test_notes_are_not_part_of_the_comparison() -> None:
    assert all(compare_extraction(TRUTH, TRUTH.model_copy(update={"notes": ("added",)})).values())


# --- anomalies -------------------------------------------------------------------------------


def test_confusion_counts() -> None:
    result = confusion({"A", "B"}, {"B", "C"})
    assert result == Confusion(true_positive=1, false_positive=1, false_negative=1)
    assert result.precision == 0.5 and result.recall == 0.5 and result.f1 == 0.5


def test_nothing_expected_and_nothing_found_is_perfect() -> None:
    result = confusion(set(), set())
    assert (result.precision, result.recall, result.f1) == (1.0, 1.0, 1.0)


def test_nothing_found_when_something_is_expected_has_zero_recall() -> None:
    result = confusion({"A"}, set())
    assert (result.precision, result.recall, result.f1) == (1.0, 0.0, 0.0)


def test_confusions_add_up_for_a_micro_average() -> None:
    total = confusion({"A"}, {"A"}) + confusion({"B"}, {"C"}) + Confusion()
    assert total == Confusion(1, 1, 1)


# --- percentiles -----------------------------------------------------------------------------


def test_nearest_rank_percentiles() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile(values, 100) == 100.0
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0


def test_percentile_of_nothing_is_zero() -> None:
    assert percentile([], 95) == 0.0


def test_an_identifier_the_ground_truth_does_not_define_is_not_compared() -> None:
    """The buyer's VAT number is printed on the invoices but absent from the ground truth."""
    assert TRUTH.buyer is not None and TRUTH.buyer.vat_number is None
    fuller = TRUTH.model_copy(
        update={"buyer": TRUTH.buyer.model_copy(update={"vat_number": "FR84213033558"})}
    )
    fields = compare_extraction(TRUTH, fuller)
    assert "buyer.vat_number" not in fields
    assert all(fields.values())


def test_a_defined_identifier_is_still_compared() -> None:
    wrong = TRUTH.seller.model_copy(update={"siren": "000000000"})
    fields = compare_extraction(TRUTH, TRUTH.model_copy(update={"seller": wrong}))
    assert fields["seller.siren"] is False
