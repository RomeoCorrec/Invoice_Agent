"""The thresholds file and the comparison of a run with it."""

import re
from pathlib import Path

import pytest

from evals import run
from evals.explain_runner import ExplainResult, ExplainSummary
from evals.rescore import main as rescore_main
from evals.run import main, save_results
from evals.thresholds import (
    ThresholdsError,
    check,
    check_explain,
    explain_values,
    load_thresholds,
    report_explain,
)
from invoice_agent.agent.explanation import AnomalyExplanation, Explanation
from invoice_agent.agent.graph.deps import ExplainConfig
from invoice_agent.llm.prompt import load_prompt
from tests.fakes import FakeToolProvider


def answered(case_id: str = "INV-12", **changes: object) -> ExplainResult:
    base = ExplainResult(
        case_id=case_id,
        status="ok",
        given=("A", "B"),
        explained=("A", "B"),
        coverage=1.0,
        cost_usd=0.0006,
        latency_s=3.0,
    )
    from dataclasses import replace

    return replace(base, **changes)


def summary(*results: ExplainResult) -> ExplainSummary:
    return ExplainSummary(model="m", prompt="p", results=results)


# --- the file --------------------------------------------------------------------------------


def test_the_committed_thresholds_are_the_agreed_ones() -> None:
    limits = load_thresholds()["explain"]
    assert limits == {
        "min_availability": 0.90,
        "min_coverage": 0.90,
        "min_no_invented_code": 0.95,
        "min_no_ungrounded_figure": 0.85,
        "min_tool_discipline": 1.0,
        "min_injection_ok": 1.0,
        "max_cost_per_case_usd": 0.01,
    }


def test_every_threshold_of_the_file_talks_about_a_metric_that_exists() -> None:
    values = explain_values(summary(answered()))
    limits = load_thresholds()["explain"]
    assert check(values, limits) == []  # also fails if a name is unknown


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("explain:\n  min_coverage: high\n", "not a number"),
        ("explain:\n  coverage: 0.9\n", "expected 'min_<metric>"),
        ("  min_coverage: 0.9\n", "outside any section"),
        ("explain\n  min_coverage: 0.9\n", "section name"),
        ("explain:\n  - min_coverage\n", "expected 'min_<metric>"),
    ],
)
def test_anything_outside_the_supported_subset_is_an_error(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "thresholds.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ThresholdsError, match=message):
        load_thresholds(path)


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.yaml"
    path.write_text("# a comment\n\nexplain:  # the suite\n  min_coverage: 0.5  # half\n")
    assert load_thresholds(path) == {"explain": {"min_coverage": 0.5}}


# --- the comparison --------------------------------------------------------------------------


def test_a_run_that_meets_every_threshold_has_no_violation() -> None:
    assert check_explain(summary(answered(), answered("INV-13"))) == []


def test_each_limit_is_applied_in_its_direction() -> None:
    lazy = answered(explained=("A",), coverage=0.5)
    bad = check_explain(summary(lazy))
    assert [v.name for v in bad] == ["coverage"] and bad[0].kind == "min"
    assert str(bad[0]) == "coverage: 0.5 < 0.9"

    costly = check_explain(summary(answered(cost_usd=0.5)))
    assert [(v.name, v.kind) for v in costly] == [("cost_per_case_usd", "max")]
    assert str(costly[0]) == "cost_per_case_usd: 0.5 > 0.01"


def test_an_invented_code_and_a_failed_tool_rule_are_violations() -> None:
    run_with_problems = summary(
        answered(invented=("MADE_UP",)), answered("INV-13", discipline_ok=False)
    )
    names = {v.name for v in check_explain(run_with_problems)}
    assert names == {"no_invented_code", "tool_discipline"}


def test_an_unknown_metric_or_section_is_an_error_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(ThresholdsError, match="unknown metric 'speed'"):
        check({"coverage": 1.0}, {"min_speed": 1.0})
    path = tmp_path / "thresholds.yaml"
    path.write_text("extract:\n  min_field_accuracy: 0.9\n")
    with pytest.raises(ThresholdsError, match="no 'explain' section"):
        check_explain(summary(answered()), path)


def test_the_report_prints_and_returns_the_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert report_explain(summary(answered())) == 0
    assert "thresholds: all met" in capsys.readouterr().out
    assert report_explain(summary(answered(cost_usd=0.5))) == 1
    assert "cost_per_case_usd: 0.5 > 0.01" in capsys.readouterr().out
    broken = tmp_path / "thresholds.yaml"
    broken.write_text("explain:\n  min_coverage: high\n")
    assert report_explain(summary(answered()), broken) == 2
    assert "not a number" in capsys.readouterr().err


# --- the commands ----------------------------------------------------------------------------

CODE = re.compile(r'"code": "([A-Z_]+)"')


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ANTHROPIC_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("IBAN_HASH_KEY", "test-key".ljust(16, "k"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")


def scripted(monkeypatch: pytest.MonkeyPatch, keep: int | None) -> None:
    """A model that explains every anomaly (keep=None) or only the first ``keep``."""

    def reply(user: str) -> Explanation:
        codes = CODE.findall(user)[:keep]
        return Explanation(
            summary="Résumé.",
            items=[AnomalyExplanation(code=c, explanation="Explication.") for c in codes],
        )

    config = ExplainConfig(
        provider=FakeToolProvider(reply),  # type: ignore[arg-type]
        prompt=load_prompt("explain_anomalies", 1),
    )
    monkeypatch.setattr(run, "build_explain_config", lambda *_a, **_k: config)


def test_the_explain_command_succeeds_when_the_thresholds_are_met(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(monkeypatch, keep=None)
    assert main(["--suite", "explain", "--yes", "--no-langfuse"]) == 0
    assert "thresholds: all met" in capsys.readouterr().out


def test_the_explain_command_fails_when_a_model_explains_too_little(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(monkeypatch, keep=1)  # only the first anomaly of each invoice
    assert main(["--suite", "explain", "--yes", "--no-langfuse"]) == 1
    assert "coverage: 0.75 < 0.9" in capsys.readouterr().out


def test_rescoring_applies_the_thresholds_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = save_results(summary(answered()), tmp_path / "good")
    assert rescore_main([str(good)]) == 0
    bad = save_results(summary(answered(explained=("A",), coverage=0.5)), tmp_path / "bad")
    assert rescore_main([str(bad)]) == 1
    assert "coverage" in capsys.readouterr().out
