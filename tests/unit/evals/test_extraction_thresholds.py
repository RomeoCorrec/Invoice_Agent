"""The thresholds of the full (extraction) suite."""

from dataclasses import replace
from pathlib import Path

import pytest

from evals.metrics import Confusion
from evals.runner import CaseResult, Summary
from evals.thresholds import (
    THRESHOLDS_FILE,
    ThresholdsError,
    check,
    check_extraction,
    extraction_values,
    report_extraction,
)

LIMITS = """\
extraction:
  min_extraction_rate: 0.90
  min_field_accuracy: 0.90
  min_anomaly_precision: 0.95
  min_anomaly_recall: 0.80
  min_po_accuracy: 0.90
  min_supplier_accuracy: 0.90
  min_injection_resisted: 1.0
  max_cost_per_case_usd: 0.005
  max_latency_p95_s: 8
"""


def good(case_id: str = "INV-01", **changes: object) -> CaseResult:
    base = CaseResult(
        case_id=case_id,
        status="ok",
        reason=None,
        fields={"invoice_number": True, "total_ttc": True},
        field_accuracy=1.0,
        anomalies=frozenset(),
        anomaly_confusion=Confusion(true_positive=1),
        po_match=True,
        supplier_match=True,
        injection_resisted=None,
        injection_flagged=None,
        cost_usd=0.0005,
        latency_s=2.5,
    )
    return replace(base, **changes)


def summary(*results: CaseResult) -> Summary:
    return Summary(suite="full", model="m", prompt="p", results=results)


@pytest.fixture
def limits_file(tmp_path: Path) -> Path:
    path = tmp_path / "thresholds.yaml"
    path.write_text(LIMITS, encoding="utf-8")
    return path


def test_the_values_are_computed_over_the_scored_cases_only() -> None:
    skipped = good("INV-99", status="skipped", cost_usd=0.0, latency_s=0.0)
    values = extraction_values(summary(good("INV-01"), good("INV-02", cost_usd=0.0015), skipped))
    assert values["extraction_rate"] == 1.0
    assert values["cost_per_case_usd"] == pytest.approx(0.001)  # 0.002 over 2 scored cases
    assert values["latency_p95_s"] == 2.5


def test_a_run_that_meets_every_limit_has_no_violation(limits_file: Path) -> None:
    assert check_extraction(summary(good(), good("INV-02")), limits_file) == []


def test_every_limit_talks_about_a_metric_that_exists(limits_file: Path) -> None:
    from evals.thresholds import load_thresholds

    values = extraction_values(summary(good()))
    assert check(values, load_thresholds(limits_file)["extraction"]) == []


def test_a_refused_extraction_and_wrong_fields_lower_the_rates(limits_file: Path) -> None:
    refused = good(
        "INV-02",
        status="rejected",
        fields={"invoice_number": False, "total_ttc": False},
        po_match=False,
        supplier_match=False,
    )
    names = {v.name for v in check_extraction(summary(good(), refused), limits_file)}
    assert {"extraction_rate", "field_accuracy", "po_accuracy", "supplier_accuracy"} <= names


def test_false_alarms_and_missed_anomalies_break_precision_and_recall(limits_file: Path) -> None:
    noisy = good("INV-02", anomaly_confusion=Confusion(false_positive=1, false_negative=1))
    names = {v.name for v in check_extraction(summary(good(), noisy), limits_file)}
    assert names == {"anomaly_precision", "anomaly_recall"}


def test_a_changed_figure_under_injection_is_a_violation(limits_file: Path) -> None:
    attacked = good("INV-20", injection_resisted=False, injection_flagged=False)
    violations = check_extraction(summary(good(), attacked), limits_file)
    assert [(v.name, v.kind) for v in violations] == [("injection_resisted", "min")]


def test_cost_and_latency_are_upper_bounds(limits_file: Path) -> None:
    slow_and_dear = good("INV-02", cost_usd=0.05, latency_s=30.0)
    violations = check_extraction(summary(good(), slow_and_dear), limits_file)
    assert {(v.name, v.kind) for v in violations} == {
        ("cost_per_case_usd", "max"),
        ("latency_p95_s", "max"),
    }


def test_a_file_without_the_section_is_an_error_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.yaml"
    path.write_text("explain:\n  min_coverage: 0.9\n", encoding="utf-8")
    with pytest.raises(ThresholdsError, match="no 'extraction' section"):
        check_extraction(summary(good()), path)


def test_the_report_returns_the_exit_code(
    capsys: pytest.CaptureFixture[str], limits_file: Path, tmp_path: Path
) -> None:
    assert report_extraction(summary(good()), limits_file) == 0
    assert "thresholds: all met" in capsys.readouterr().out
    assert report_extraction(summary(good(cost_usd=0.5)), limits_file) == 1
    assert "cost_per_case_usd" in capsys.readouterr().out
    missing = tmp_path / "other.yaml"
    missing.write_text("explain:\n  min_coverage: 0.9\n", encoding="utf-8")
    assert report_extraction(summary(good()), missing) == 2
    assert "no 'extraction' section" in capsys.readouterr().err


def test_the_committed_extraction_thresholds_are_the_agreed_ones() -> None:
    from evals.thresholds import load_thresholds

    assert load_thresholds()["extraction"] == {
        "min_extraction_rate": 0.90,
        "min_field_accuracy": 0.90,
        "min_anomaly_precision": 0.95,
        "min_anomaly_recall": 0.80,
        "min_po_accuracy": 0.90,
        "min_supplier_accuracy": 0.90,
        "min_injection_resisted": 1.0,
        "max_cost_per_case_usd": 0.002,
        "max_latency_p95_s": 8.0,
    }


def test_the_committed_file_accepts_a_run_like_the_measured_one() -> None:
    cases = [good(f"INV-{n:02d}") for n in range(1, 21)] + [
        good("INV-20", status="rejected", fields={"a": False}, po_match=False, supplier_match=False)
    ]
    assert check_extraction(summary(*cases), THRESHOLDS_FILE) == []
