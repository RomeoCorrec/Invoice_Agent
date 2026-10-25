import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evals import run
from evals.run import main, save_results, worst_case_cost
from tests.factories import GROUND_TRUTH, IBAN_KEY
from tests.fakes import FakeProvider
from tests.unit.evals.test_runner import pick

ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
    "IBAN_HASH_KEY": IBAN_KEY.decode().ljust(16, "k"),
}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def test_a_dry_run_shows_the_plan_and_calls_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--suite", "smoke", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "smoke suite: 5 cases, model claude-haiku-5-5" in out
    assert "cap $2.00" in out


def test_nothing_is_sent_without_yes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--suite", "smoke"]) == 2
    assert "refusing to call the API without --yes" in capsys.readouterr().err


def test_a_run_without_an_api_key_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--suite", "smoke", "--yes"]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_missing_configuration_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("IBAN_HASH_KEY")
    assert main(["--suite", "smoke", "--dry-run"]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_the_worst_case_grows_with_the_cases_and_the_model() -> None:
    assert worst_case_cost("claude-haiku-5-5", 10) == 10 * worst_case_cost("claude-haiku-5-5", 1)
    assert worst_case_cost("claude-sonnet-5-5", 1) > worst_case_cost("claude-haiku-5-5", 1) > 0
    assert worst_case_cost("claude-haiku-5-5", 21) < Decimal("0.2")


def test_a_full_run_with_a_scripted_provider_reports_and_saves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")

    def scripted(**_kwargs: Any) -> FakeProvider:
        return FakeProvider(pick)

    monkeypatch.setattr(run, "AnthropicProvider", scripted)
    report = tmp_path / "docs" / "latest.md"
    code = main(["--suite", "smoke", "--yes", "--no-langfuse", "--report", str(report)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Field accuracy (micro)" in out
    assert "results saved to" in out
    [saved] = (tmp_path / "results").glob("*.json")
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["suite"] == "smoke"
    assert payload["model"] == "claude-haiku-5-5"
    assert {r["case_id"] for r in payload["results"]} == {
        "INV-09",
        "INV-10",
        "INV-11",
        "INV-20",
        "INV-21",
    }
    assert report.read_text(encoding="utf-8").startswith("# Evaluation: smoke suite")
    assert (GROUND_TRUTH / "INV-09.json").exists()


def test_the_cost_cap_is_honoured_by_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(run, "AnthropicProvider", lambda **_kw: FakeProvider(pick))
    assert main(["--suite", "smoke", "--yes", "--no-langfuse", "--max-cost", "0.0003"]) == 0
    assert "skipped" in capsys.readouterr().out


def test_results_files_are_valid_json_with_sets_and_decimals(tmp_path: Path) -> None:
    from evals.metrics import Confusion
    from evals.runner import CaseResult, Summary

    result = CaseResult(
        case_id="INV-01",
        status="ok",
        reason=None,
        fields={"invoice_number": True},
        field_accuracy=1.0,
        anomalies=frozenset({"B", "A"}),
        anomaly_confusion=Confusion(),
        po_match=True,
        supplier_match=True,
        injection_resisted=None,
        injection_flagged=None,
    )
    path = save_results(Summary("smoke", "claude-haiku-5-5", "p", (result,)), tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["results"][0]["anomalies"] == ["A", "B"]
