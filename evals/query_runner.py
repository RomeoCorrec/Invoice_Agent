"""Run the model that polishes the message to a supplier on the golden cases.

Each case goes through the production path up to the model: the extraction (from the ground
truth, so only the draft is under test), the rules, the facts that may be said to a supplier,
and the production ``make_draft`` with its checks. A draft the checks refuse falls back to the
template, as in production, and the evaluation counts it: the share of drafts the model gets
through the checks is what the prompt is measured by. Every case is drafted twice, as a first
message and as a reminder, since the prompt covers both.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from evals.context import Reference
from evals.dataset import EvalCase
from evals.explain_metrics import directives
from evals.explain_runner import GoldenExtractor
from evals.metrics import percentile
from evals.runner import CostBudget
from invoice_agent.agent.checks import run_checks
from invoice_agent.agent.correspondence.drafting import make_draft
from invoice_agent.agent.correspondence.facts import build_facts
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.pipeline import extract_from_text
from invoice_agent.llm.prompt import Prompt
from invoice_agent.llm.provider import LlmProvider


@dataclass(frozen=True)
class QueryResult:
    case_id: str
    reminder: bool
    status: str
    """ok (a draft was made), not_reached (the pipeline stopped before the model) or skipped."""
    reason: str | None = None
    source: str = ""
    """``model`` when its draft passed every check, ``template`` otherwise."""
    fallback_reason: str | None = None
    problems: tuple[str, ...] = ()
    directive_hits: tuple[str, ...] = ()
    subject: str = ""
    body: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0


def run_query_case(
    case: EvalCase,
    provider: LlmProvider,
    prompt: Prompt,
    reference: Reference,
    iban_key: bytes,
    *,
    reminder: bool,
) -> QueryResult:
    try:
        data = extract_from_text(case.text, GoldenExtractor(case.expected), iban_key)
    except ExtractionError as exc:
        return QueryResult(
            case.case_id, reminder, "not_reached", f"refused by the pipeline: {exc}"[:200]
        )
    findings = [
        a.model_dump(mode="json") for a in run_checks(data, reference.context_for(data)).anomalies
    ]
    if not findings:
        return QueryResult(case.case_id, reminder, "not_reached", "the rules found nothing")
    facts = build_facts(data, findings, reminder=reminder)
    started = time.perf_counter()
    drafted = make_draft(facts, provider=provider, prompt=prompt)
    elapsed = time.perf_counter() - started
    text = f"{drafted.draft.subject}\n{drafted.draft.body}"
    return QueryResult(
        case_id=case.case_id,
        reminder=reminder,
        status="ok",
        source=drafted.source,
        fallback_reason=drafted.fallback_reason,
        problems=drafted.problems,
        directive_hits=tuple(directives(text)),
        subject=drafted.draft.subject,
        body=drafted.draft.body,
        cost_usd=float(drafted.cost_usd),
        latency_s=elapsed,
    )


def skipped(case: EvalCase, reminder: bool) -> QueryResult:
    return QueryResult(case.case_id, reminder, "skipped", "cost cap reached")


@dataclass(frozen=True)
class QuerySummary:
    model: str
    prompt: str
    results: Sequence[QueryResult] = ()
    suite: str = "query"

    @property
    def drafted(self) -> list[QueryResult]:
        return [r for r in self.results if r.status == "ok"]

    @staticmethod
    def _share(flags: Sequence[bool]) -> float:
        return sum(flags) / len(flags) if flags else 0.0

    @property
    def accepted(self) -> float:
        """Drafts of the model that passed every check."""
        return self._share([r.source == "model" for r in self.drafted])

    @property
    def available(self) -> float:
        """Drafts that are not a fallback because the model could not be reached."""
        return self._share([r.fallback_reason != "model unavailable" for r in self.drafted])

    @property
    def directive_free(self) -> float:
        return self._share([not r.directive_hits for r in self.drafted])

    @property
    def cost_usd(self) -> Decimal:
        return sum((Decimal(str(r.cost_usd)) for r in self.results), start=Decimal(0))

    @property
    def latency_p50(self) -> float:
        return percentile([r.latency_s for r in self.drafted], 50)

    @property
    def latency_p95(self) -> float:
        return percentile([r.latency_s for r in self.drafted], 95)


def run_query_suite(
    cases: Sequence[EvalCase],
    provider: LlmProvider,
    prompt: Prompt,
    reference: Reference,
    iban_key: bytes,
    budget: CostBudget,
    *,
    model: str,
) -> QuerySummary:
    results: list[QueryResult] = []
    for case in cases:
        for reminder in (False, True):
            if budget.exhausted:
                results.append(skipped(case, reminder))
                continue
            result = run_query_case(case, provider, prompt, reference, iban_key, reminder=reminder)
            budget.charge(result.cost_usd)
            results.append(result)
    return QuerySummary(model=model, prompt=prompt.label, results=tuple(results))
