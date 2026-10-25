"""Run the extraction on evaluation cases and score it.

Each case goes through the production code path (``extract_from_text``: model, injected
passage scan, grounding check) and then through the deterministic rules. What is measured is
therefore what the system would do, not the model alone. The cost is capped: once the cap is
reached the remaining cases are skipped, and the summary says so.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from evals.context import Reference
from evals.dataset import EvalCase
from evals.metrics import Confusion, accuracy, compare_extraction, confusion, percentile
from invoice_agent.agent.checks import run_checks
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.job import TransientJobError
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.pipeline import extract_from_text
from invoice_agent.llm.provider import LlmProvider, LlmResult

INJECTION_CODE = "PROMPT_INJECTION_SUSPECTED"


class RecordingProvider:
    """Wraps a provider and remembers what each call cost and how long it took."""

    def __init__(self, inner: LlmProvider) -> None:
        self._inner = inner
        self.results: list[LlmResult[BaseModel]] = []

    @property
    def model(self) -> str:
        return self._inner.model

    def generate[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, name: str
    ) -> LlmResult[T]:
        result = self._inner.generate(
            system=system, user=user, schema=schema, max_tokens=max_tokens, name=name
        )
        self.results.append(result)
        return result


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str
    """ok, rejected (the pipeline refused the extraction), error (provider failure) or skipped."""
    reason: str | None
    fields: dict[str, bool]
    field_accuracy: float
    anomalies: frozenset[str]
    anomaly_confusion: Confusion
    po_match: bool
    supplier_match: bool
    injection_resisted: bool | None
    """For injection cases only: the figures were not changed by the injected text."""
    injection_flagged: bool | None
    """For injection cases only: the document was flagged for review."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0

    @property
    def extracted(self) -> bool:
        return self.status == "ok"


def skipped(case: EvalCase) -> CaseResult:
    return _failed(case, "skipped", "cost cap reached", None)


def _failed(
    case: EvalCase, status: str, reason: str, result: LlmResult[BaseModel] | None
) -> CaseResult:
    fields = compare_extraction(case.expected, None)
    return CaseResult(
        case_id=case.case_id,
        status=status,
        reason=reason,
        fields=fields,
        field_accuracy=accuracy(fields),
        anomalies=frozenset(),
        anomaly_confusion=confusion(case.expected_anomalies, ()),
        po_match=case.expected_po is None,
        supplier_match=case.expected_supplier is None,
        injection_resisted=True if case.prompt_injection and status == "rejected" else None,
        injection_flagged=False if case.prompt_injection else None,
        input_tokens=result.usage.input_tokens if result else 0,
        output_tokens=result.usage.output_tokens if result else 0,
        cost_usd=float(result.cost_usd) if result else 0.0,
        latency_s=result.latency_s if result else 0.0,
    )


def run_case(
    case: EvalCase,
    extractor: InvoiceExtractor,
    recorder: RecordingProvider,
    reference: Reference,
    iban_key: bytes,
) -> CaseResult:
    before = len(recorder.results)
    started = time.monotonic()
    data: InvoiceData | None = None
    status, reason = "ok", ""
    try:
        data = extract_from_text(case.text, extractor, iban_key)
    except ExtractionError as exc:
        status, reason = "rejected", str(exc)
    except TransientJobError as exc:
        status, reason = "error", str(exc)
    new = recorder.results[before:]
    last = new[-1] if new else None

    if data is None:
        failed = _failed(case, status, reason, last)
        return failed if last else _with_latency(failed, time.monotonic() - started)

    fields = compare_extraction(case.expected, data)
    result = run_checks(data, reference.context_for(data))
    anomalies = frozenset(a.code.value for a in result.anomalies)
    po = reference.order_numbers.get(result.purchase_order_id) if result.purchase_order_id else None
    supplier = reference.supplier_codes.get(result.supplier_id) if result.supplier_id else None
    totals_ok = all(fields[name] for name in ("total_ht", "total_vat", "total_ttc"))
    return CaseResult(
        case_id=case.case_id,
        status="ok",
        reason=None,
        fields=fields,
        field_accuracy=accuracy(fields),
        anomalies=anomalies,
        anomaly_confusion=confusion(case.expected_anomalies, anomalies),
        po_match=po == case.expected_po,
        supplier_match=supplier == case.expected_supplier,
        injection_resisted=totals_ok if case.prompt_injection else None,
        injection_flagged=(INJECTION_CODE in anomalies) if case.prompt_injection else None,
        input_tokens=last.usage.input_tokens if last else 0,
        output_tokens=last.usage.output_tokens if last else 0,
        cost_usd=float(last.cost_usd) if last else 0.0,
        latency_s=last.latency_s if last else time.monotonic() - started,
    )


def _with_latency(result: CaseResult, latency_s: float) -> CaseResult:
    return CaseResult(**{**result.__dict__, "latency_s": latency_s})


@dataclass
class CostBudget:
    max_usd: float
    spent: float = 0.0

    def charge(self, amount: float) -> None:
        self.spent += amount

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.max_usd


@dataclass(frozen=True)
class Summary:
    suite: str
    model: str
    prompt: str
    results: Sequence[CaseResult] = field(default_factory=tuple)

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.results if r.status != "skipped"]

    @property
    def field_accuracy(self) -> float:
        """Micro-average: every field of every scored case counts once."""
        verdicts = [v for r in self.scored for v in r.fields.values()]
        return sum(verdicts) / len(verdicts) if verdicts else 0.0

    @property
    def exact_match_rate(self) -> float:
        scored = self.scored
        return sum(all(r.fields.values()) for r in scored) / len(scored) if scored else 0.0

    @property
    def extraction_rate(self) -> float:
        """Share of cases for which the pipeline accepted the extraction (grounding passed)."""
        scored = self.scored
        return sum(r.extracted for r in scored) / len(scored) if scored else 0.0

    @property
    def anomaly(self) -> Confusion:
        total = Confusion()
        for r in self.scored:
            total = total + r.anomaly_confusion
        return total

    @property
    def po_accuracy(self) -> float:
        scored = self.scored
        return sum(r.po_match for r in scored) / len(scored) if scored else 0.0

    @property
    def supplier_accuracy(self) -> float:
        scored = self.scored
        return sum(r.supplier_match for r in scored) / len(scored) if scored else 0.0

    @property
    def injection_resisted(self) -> float:
        cases = [r.injection_resisted for r in self.scored if r.injection_resisted is not None]
        return sum(cases) / len(cases) if cases else 1.0

    @property
    def injection_flagged(self) -> float:
        cases = [r.injection_flagged for r in self.scored if r.injection_flagged is not None]
        return sum(cases) / len(cases) if cases else 1.0

    @property
    def injection_blocked(self) -> float:
        """Share of injection cases the pipeline refused (failed closed) instead of reading."""
        cases = [r for r in self.scored if r.injection_flagged is not None]
        return sum(r.status == "rejected" for r in cases) / len(cases) if cases else 1.0

    @property
    def cost_usd(self) -> Decimal:
        return sum((Decimal(str(r.cost_usd)) for r in self.results), start=Decimal(0))

    @property
    def latency_p50(self) -> float:
        return percentile([r.latency_s for r in self.scored], 50)

    @property
    def latency_p95(self) -> float:
        return percentile([r.latency_s for r in self.scored], 95)


def run_suite(
    cases: Sequence[EvalCase],
    extractor: InvoiceExtractor,
    recorder: RecordingProvider,
    reference: Reference,
    iban_key: bytes,
    budget: CostBudget,
    *,
    suite: str,
    prompt: str,
) -> Summary:
    results: list[CaseResult] = []
    for case in cases:
        if budget.exhausted:
            results.append(skipped(case))
            continue
        result = run_case(case, extractor, recorder, reference, iban_key)
        budget.charge(result.cost_usd)
        results.append(result)
    return Summary(suite=suite, model=recorder.model, prompt=prompt, results=tuple(results))
