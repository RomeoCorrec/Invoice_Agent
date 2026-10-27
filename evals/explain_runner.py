"""Run the explaining model on the golden cases and score what it wrote.

Each case goes through the production path up to the model: the extraction (from the ground
truth, so only the explanation is under test), the injected-passage scan, the grounding check
and the rules. The case file is built by the production code, and the model is called by the
production ``ask_model`` with the production tools, reading from the dataset's reference data.
A case the pipeline refuses (INV-20) never reaches the model, as in production.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from evals.context import Reference
from evals.dataset import EvalCase
from evals.explain_metrics import (
    coverage,
    directives,
    invented_codes,
    tool_discipline,
    ungrounded_figures,
)
from evals.memory_ledger import MemoryLedger
from evals.metrics import percentile
from evals.runner import CostBudget
from invoice_agent.agent.checks import run_checks
from invoice_agent.agent.explanation import build_case, keep_known_codes
from invoice_agent.agent.graph.deps import ExplainConfig
from invoice_agent.agent.graph.explain_node import ask_model, read_only_tools
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.pipeline import extract_from_text
from invoice_agent.llm.provider import LlmError
from invoice_agent.llm.tools import ToolSpec
from invoice_agent.tools.errors import ToolError

INJECTION_CODE = "PROMPT_INJECTION_SUSPECTED"


class GoldenExtractor:
    """Plays the extraction model with the ground truth: only the explanation is evaluated."""

    def __init__(self, data: InvoiceData) -> None:
        self._data = data

    def extract(self, text: str) -> InvoiceData:
        return self._data


@dataclass(frozen=True)
class ExplainResult:
    case_id: str
    status: str
    """ok, unavailable (the model or the ledger failed), not_reached (the pipeline stopped
    before the model) or skipped (cost cap)."""
    reason: str | None = None
    given: tuple[str, ...] = ()
    explained: tuple[str, ...] = ()
    coverage: float = 0.0
    invented: tuple[str, ...] = ()
    ungrounded: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    tool_errors: int = 0
    discipline_ok: bool = True
    directive_hits: tuple[str, ...] = ()
    injection_ok: bool | None = None
    summary: str = ""
    items: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


def _recording(tools: list[ToolSpec]) -> tuple[list[ToolSpec], list[str]]:
    """The same tools, remembering what they returned (to know which figures are grounded)."""
    outputs: list[str] = []

    def wrap(tool: ToolSpec) -> ToolSpec:
        def handler(arguments: dict[str, Any]) -> str:
            text = tool.handler(arguments)
            outputs.append(text)
            return text

        return ToolSpec(tool.name, tool.description, tool.input_schema, handler)

    return [wrap(tool) for tool in tools], outputs


def run_explain_case(
    case: EvalCase,
    config: ExplainConfig,
    ledger: MemoryLedger,
    reference: Reference,
    iban_key: bytes,
) -> ExplainResult:
    try:
        data = extract_from_text(case.text, GoldenExtractor(case.expected), iban_key)
    except ExtractionError as exc:
        return ExplainResult(case.case_id, "not_reached", f"refused by the pipeline: {exc}"[:200])
    findings = [
        a.model_dump(mode="json") for a in run_checks(data, reference.context_for(data)).anomalies
    ]
    given = tuple(str(f["code"]) for f in findings)
    if not findings:
        return ExplainResult(case.case_id, "not_reached", "the rules found nothing to explain")

    case_text = build_case(data, findings)
    tools, outputs = _recording(read_only_tools(ledger))
    try:
        result = ask_model(config, case_text, tools)
    except (LlmError, ToolError) as failure:
        return ExplainResult(case.case_id, "unavailable", str(failure)[:200], given=given)

    raw = result.parsed
    checked = keep_known_codes(raw, set(given))
    explained = tuple(item.code for item in checked.explanation.items)
    body = " ".join([raw.summary, *(item.explanation for item in raw.items)])
    ungrounded = tuple(ungrounded_figures(body, [case_text, *outputs]))
    hits = tuple(directives(body))
    names = [call.name for call in result.tool_calls]
    return ExplainResult(
        case_id=case.case_id,
        status="ok",
        given=given,
        explained=explained,
        coverage=coverage(given, explained),
        invented=tuple(invented_codes(given, [item.code for item in raw.items])),
        ungrounded=ungrounded,
        tools=tuple(names),
        tool_errors=sum(not call.ok for call in result.tool_calls),
        discipline_ok=tool_discipline(names, budget=config.max_tool_calls),
        directive_hits=hits,
        injection_ok=(
            INJECTION_CODE in explained and not ungrounded and not hits
            if case.prompt_injection
            else None
        ),
        summary=checked.explanation.summary,
        items=tuple((item.code, item.explanation) for item in checked.explanation.items),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=float(result.cost_usd),
        latency_s=result.latency_s,
    )


def skipped(case: EvalCase) -> ExplainResult:
    return ExplainResult(case.case_id, "skipped", "cost cap reached")


@dataclass(frozen=True)
class ExplainSummary:
    model: str
    prompt: str
    results: Sequence[ExplainResult] = ()
    suite: str = "explain"

    @property
    def answered(self) -> list[ExplainResult]:
        return [r for r in self.results if r.status == "ok"]

    @property
    def attempted(self) -> list[ExplainResult]:
        return [r for r in self.results if r.status in ("ok", "unavailable")]

    @staticmethod
    def _share(flags: Sequence[bool]) -> float:
        return sum(flags) / len(flags) if flags else 0.0

    @property
    def availability(self) -> float:
        return self._share([r.status == "ok" for r in self.attempted])

    @property
    def coverage(self) -> float:
        """Micro-average: every anomaly given to the model counts once."""
        given = sum(len(r.given) for r in self.answered)
        covered = sum(round(r.coverage * len(r.given)) for r in self.answered)
        return covered / given if given else 0.0

    @property
    def clean_of_invented_codes(self) -> float:
        return self._share([not r.invented for r in self.answered])

    @property
    def clean_of_ungrounded_figures(self) -> float:
        return self._share([not r.ungrounded for r in self.answered])

    @property
    def tool_discipline(self) -> float:
        return self._share([r.discipline_ok for r in self.answered])

    @property
    def injection_ok(self) -> float:
        flags = [r.injection_ok for r in self.answered if r.injection_ok is not None]
        return self._share([bool(flag) for flag in flags]) if flags else 1.0

    @property
    def directive_free(self) -> float:
        return self._share([not r.directive_hits for r in self.answered])

    @property
    def tools_per_case(self) -> float:
        return (
            sum(len(r.tools) for r in self.answered) / len(self.answered) if self.answered else 0.0
        )

    @property
    def cost_usd(self) -> Decimal:
        return sum((Decimal(str(r.cost_usd)) for r in self.results), start=Decimal(0))

    @property
    def latency_p50(self) -> float:
        return percentile([r.latency_s for r in self.answered], 50)

    @property
    def latency_p95(self) -> float:
        return percentile([r.latency_s for r in self.answered], 95)


def run_explain_suite(
    cases: Sequence[EvalCase],
    config: ExplainConfig,
    reference: Reference,
    iban_key: bytes,
    budget: CostBudget,
    *,
    model: str,
) -> ExplainSummary:
    ledger = MemoryLedger(reference)
    results: list[ExplainResult] = []
    for case in cases:
        if budget.exhausted:
            results.append(skipped(case))
            continue
        result = run_explain_case(case, config, ledger, reference, iban_key)
        budget.charge(result.cost_usd)
        results.append(result)
    return ExplainSummary(model=model, prompt=config.prompt.label, results=tuple(results))
