"""The query evaluation with scripted models: what it rewards and what it catches."""

import json
from typing import Any

import pytest

from evals.context import Reference, load_reference
from evals.dataset import EvalCase, load_cases, select
from evals.query_report import render_markdown, render_text
from evals.query_runner import QuerySummary, run_query_suite
from evals.runner import CostBudget
from evals.thresholds import query_values
from invoice_agent.agent.correspondence.drafting import QueryDraft
from invoice_agent.agent.correspondence.facts import AnomalyFact, QueryFacts
from invoice_agent.agent.correspondence.templates import template_draft
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.llm.provider import LlmPermanentError
from tests.factories import IBAN_KEY
from tests.fakes import FakeProvider


@pytest.fixture(scope="module")
def cases() -> list[EvalCase]:
    return select(load_cases(IBAN_KEY), "query")


@pytest.fixture(scope="module")
def reference() -> Reference:
    return load_reference(IBAN_KEY)


def facts_of(user: str) -> QueryFacts:
    case = json.loads(user.removeprefix("<case>").removesuffix("</case>"))
    invoice = case["invoice"]
    return QueryFacts(
        invoice_number=invoice["number"],
        purchase_order_ref=invoice["purchase_order_ref"],
        currency=invoice["currency"],
        total_ttc=invoice["total_ttc"],
        anomalies=tuple(AnomalyFact(a["code"], a["message"]) for a in case["anomalies"]),
        reminder=case["reminder"],
    )


def faithful(user: str) -> QueryDraft:
    """A model that writes what the template would: it passes every check."""
    return template_draft(facts_of(user))


def evaluate(cases: list[EvalCase], reference: Reference, reply: Any, cap: float = 5.0) -> Any:
    provider = FakeProvider(reply)
    prompt = load_prompt("draft_supplier_query", 1)
    return run_query_suite(
        cases, provider, prompt, reference, IBAN_KEY, CostBudget(cap), model="claude-haiku-5-5"
    )  # type: ignore[arg-type]


def test_every_case_is_drafted_as_a_first_message_and_as_a_reminder(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, faithful)
    assert len(summary.results) == 2 * len(cases)
    assert {r.reminder for r in summary.results} == {False, True}
    refused = [r for r in summary.results if r.status == "not_reached"]
    assert {r.case_id for r in refused} == {"INV-20"}  # refused before the model, as in production


def test_a_faithful_model_is_accepted_on_every_draft(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, faithful)
    assert summary.accepted == summary.available == summary.directive_free == 1.0
    values = query_values(summary)
    assert values["accepted"] == 1.0 and values["cost_per_draft_usd"] >= 0


def test_a_draft_the_checks_refuse_falls_back_to_the_template_and_is_counted(
    cases: list[EvalCase], reference: Reference
) -> None:
    careless = QueryDraft(subject="Facture", body="Nous paierons demain, promis.", addressed=())
    summary = evaluate(cases, reference, careless)
    assert summary.accepted == 0.0 and summary.available == 1.0
    assert all(r.source == "template" and r.problems for r in summary.drafted)


def test_a_model_that_cannot_be_reached_is_counted_apart_from_a_bad_draft(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary = evaluate(cases, reference, LlmPermanentError("down"))
    assert summary.available == 0.0 and summary.accepted == 0.0
    assert all(r.fallback_reason == "model unavailable" for r in summary.drafted)


def test_the_cost_cap_skips_the_rest(cases: list[EvalCase], reference: Reference) -> None:
    summary = evaluate(cases, reference, faithful, cap=0.0)
    assert {r.status for r in summary.results} <= {"skipped", "not_reached"}
    assert not summary.drafted


def test_the_reports_list_every_draft_and_what_the_model_wrote(
    cases: list[EvalCase], reference: Reference
) -> None:
    summary: QuerySummary = evaluate(cases, reference, faithful)
    text = render_text(summary)
    assert "Model draft passed every check" in text and "INV-12" in text
    markdown = render_markdown(summary, date="2026-10-30")
    assert "# Evaluation: query suite" in markdown and "What the model wrote" in markdown
