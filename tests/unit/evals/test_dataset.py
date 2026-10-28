import pytest

from evals.dataset import SMOKE_CASES, load_cases, select
from tests.factories import IBAN_KEY


@pytest.fixture(scope="module")
def cases():  # type: ignore[no-untyped-def]
    return load_cases(IBAN_KEY)


def test_the_29_golden_cases_are_loaded_with_the_text_the_model_would_read(cases) -> None:  # type: ignore[no-untyped-def]
    assert len(cases) == 29
    assert all(case.text for case in cases)
    assert "Cabinet Delorme Conseil" in next(c for c in cases if c.case_id == "INV-09").text


def test_eight_cases_are_plain_pdfs_and_three_are_injections(cases) -> None:  # type: ignore[no-untyped-def]
    assert sorted(c.case_id for c in cases if c.plain_pdf) == [
        "INV-09",
        "INV-10",
        "INV-11",
        "INV-21",
        "INV-22",
        "INV-23",
        "INV-28",
        "INV-29",
    ]
    assert sorted(c.case_id for c in cases if c.prompt_injection) == [
        "INV-20",
        "INV-21",
        "INV-29",
    ]


def test_history_dependent_anomalies_are_not_expected(cases) -> None:  # type: ignore[no-untyped-def]
    by_id = {c.case_id: c for c in cases}
    assert by_id["INV-14"].expected_anomalies == frozenset()  # a duplicate needs history
    assert by_id["INV-21"].expected_anomalies == {"PROMPT_INJECTION_SUSPECTED"}
    assert by_id["INV-12"].expected_anomalies == {"VAT_AMOUNT_MISMATCH"}
    assert by_id["INV-20"].expected_anomalies == {"AMOUNT_EXCEEDS_PO", "PROMPT_INJECTION_SUSPECTED"}
    assert by_id["INV-29"].expected_anomalies == {"PROMPT_INJECTION_SUSPECTED"}
    assert by_id["INV-26"].expected_anomalies == {"AMOUNT_EXCEEDS_PO"}  # a cent over the order


def test_suites(cases) -> None:  # type: ignore[no-untyped-def]
    assert len(select(cases, "full")) == 29
    assert [c.case_id for c in select(cases, "smoke")] == sorted(SMOKE_CASES)
    with pytest.raises(ValueError, match="unknown suite"):
        select(cases, "huge")
