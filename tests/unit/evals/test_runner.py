from decimal import Decimal
from typing import Any

import pytest

from evals.context import Reference, load_reference
from evals.dataset import EvalCase, load_cases, select
from evals.report import render_markdown, render_text
from evals.runner import CostBudget, RecordingProvider, Summary, run_suite
from invoice_agent.domain.rules.injection import squash_text
from invoice_agent.llm.extractor import LlmInvoiceExtractor
from invoice_agent.llm.provider import LlmTransientError
from tests.factories import GROUND_TRUTH, IBAN_KEY
from tests.fakes import FakeProvider
from tests.unit.llm.test_extractor import reply_for

REPLIES = {
    reply_for(p).invoice_number: reply_for(p) for p in sorted(GROUND_TRUTH.glob("INV-*.json"))
}


@pytest.fixture(scope="module")
def cases() -> list[EvalCase]:
    return load_cases(IBAN_KEY)


@pytest.fixture(scope="module")
def reference() -> Reference:
    return load_reference(IBAN_KEY)


def pick(user: str, edit: Any = None) -> Any:
    printed = f" {squash_text(user)} "
    for number, reply in REPLIES.items():
        if f" {squash_text(number)} " in printed:
            return edit(number, reply) if edit else reply
    raise KeyError("unknown document")


def evaluate(
    cases: list[EvalCase],
    reference: Reference,
    reply: Any,
    *,
    suite: str = "full",
    cap: float = 5.0,
) -> Summary:
    recorder = RecordingProvider(FakeProvider(reply))
    extractor = LlmInvoiceExtractor(recorder, IBAN_KEY)
    return run_suite(
        select(cases, suite),
        extractor,
        recorder,
        reference,
        IBAN_KEY,
        CostBudget(cap),
        suite=suite,
        prompt=extractor.prompt.label,
    )


def by_id(summary: Summary) -> dict[str, Any]:
    return {r.case_id: r for r in summary.results}


def test_a_perfect_model_gets_everything_right_except_what_the_pipeline_refuses(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, pick)
    results = by_id(summary)
    refused = [r.case_id for r in summary.results if r.status != "ok"]
    # INV-20 hides its injection inside a table row: the grounding check fails closed
    assert refused == ["INV-20"]
    assert results["INV-20"].status == "rejected"
    accepted = [r for r in summary.results if r.status == "ok"]
    assert all(r.field_accuracy == 1.0 for r in accepted)
    assert summary.anomaly.precision == 1.0
    assert all(r.po_match and r.supplier_match for r in accepted)
    assert summary.extraction_rate == pytest.approx(20 / 21)


def test_the_injection_cases_are_either_flagged_or_blocked_and_never_obeyed(
    cases: list[EvalCase], reference: Reference
) -> None:
    results = by_id(evaluate(cases, reference, pick))
    assert results["INV-21"].injection_flagged is True  # the note is found in the text
    assert results["INV-21"].injection_resisted is True
    assert results["INV-20"].injection_flagged is False
    assert results["INV-20"].injection_resisted is True  # refused, so its figures were not read


def test_a_model_that_obeys_the_injected_note_is_blocked(
    cases: list[EvalCase], reference: Reference
) -> None:
    def obey(number: str, reply: Any) -> Any:
        if number != "LP-0099355":  # INV-21
            return reply
        return reply.model_copy(
            update={"total_ht": "64.00", "total_vat": "12.80", "total_ttc": "76.80"}
        )

    summary = evaluate(cases, reference, lambda user: pick(user, obey))
    inv21 = by_id(summary)["INV-21"]
    assert inv21.status == "rejected"
    assert "total_ht" in (inv21.reason or "")
    assert inv21.injection_resisted is True
    assert summary.injection_blocked == 1.0


def test_values_swapped_but_printed_pass_grounding_and_cost_accuracy(
    cases: list[EvalCase], reference: Reference
) -> None:
    """Both amounts are printed on the invoice, so only the field comparison notices."""

    def swap(number: str, reply: Any) -> Any:
        if number != "DC/2026/101" and "101" not in number:
            return reply
        return reply.model_copy(update={"total_ht": reply.total_ttc, "total_ttc": reply.total_ht})

    summary = evaluate(cases, reference, lambda user: pick(user, swap), suite="smoke")
    inv09 = by_id(summary)["INV-09"]
    assert inv09.status == "ok"
    assert [name for name, ok in inv09.fields.items() if not ok] == ["total_ht", "total_ttc"]
    assert "TOTAL_MISMATCH" in inv09.anomalies  # the wrong figures also raise a false alarm
    assert summary.anomaly.precision < 1.0
    assert summary.field_accuracy < 1.0


def test_a_provider_outage_is_an_error_case_not_a_crash(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(
        cases,
        reference,
        lambda _u: LlmTransientError("model provider unavailable (HTTP 529)"),
        suite="smoke",
    )
    assert {r.status for r in summary.results} == {"error"}
    assert all("529" in (r.reason or "") for r in summary.results)
    assert summary.cost_usd == Decimal(0)
    assert summary.extraction_rate == 0.0


def test_the_cost_cap_skips_the_remaining_cases(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, pick, cap=0.0003)  # a bit more than one case
    statuses = [r.status for r in summary.results]
    assert statuses.count("skipped") == 21 - statuses.index("skipped")
    assert statuses[0] == "ok"
    assert len(summary.scored) < 21
    assert summary.cost_usd <= Decimal("0.001")
    assert all(r.cost_usd == 0.0 for r in summary.results if r.status == "skipped")


def test_every_call_is_priced_and_recorded(cases: list[EvalCase], reference: Reference) -> None:
    summary = evaluate(cases, reference, pick, suite="smoke")
    assert all(r.input_tokens > 0 and r.output_tokens == 300 for r in summary.results)
    assert summary.cost_usd > 0
    assert summary.latency_p95 >= summary.latency_p50 >= 0


def test_the_reports_show_the_metrics_and_every_case(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, pick, suite="smoke")
    text = render_text(summary)
    assert "Field accuracy (micro)" in text and "INV-09" in text and "claude-haiku-5-5" in text
    markdown = render_markdown(summary, date="2026-10-25")
    assert markdown.startswith("# Evaluation: smoke suite")
    assert "| INV-21 | ok |" in markdown
    assert "| Anomaly F1 |" in markdown
