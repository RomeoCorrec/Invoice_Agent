"""Run an evaluation as a Langfuse experiment on the golden dataset.

The dataset lives in Langfuse (one item per invoice, created idempotently), each run is an
experiment named after the suite, the model and the prompt, and every case becomes a trace
with its scores, so runs can be compared side by side in the UI.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from langfuse import Evaluation, Langfuse

from evals.dataset import EvalCase
from evals.runner import CaseResult, Summary

DATASET_NAME = "invoice-golden"


def item_id(case_id: str) -> str:
    return f"golden-{case_id}"


def sync_dataset(client: Langfuse, cases: Sequence[EvalCase]) -> None:
    """Create the dataset and its items if they are missing; update them otherwise."""
    try:
        client.get_dataset(DATASET_NAME)
    except Exception:  # the SDK raises when the dataset does not exist yet
        client.create_dataset(
            name=DATASET_NAME,
            description="Golden invoices: the text the model reads and the expected extraction.",
        )
    for case in cases:
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=item_id(case.case_id),
            input={"case_id": case.case_id, "title": case.title, "text": case.text},
            expected_output=case.expected.model_dump(mode="json"),
            metadata={
                "plain_pdf": case.plain_pdf,
                "prompt_injection": case.prompt_injection,
                "expected_anomalies": sorted(case.expected_anomalies),
            },
        )


def _output(result: CaseResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "reason": result.reason,
        "field_accuracy": result.field_accuracy,
        "anomalies": sorted(result.anomalies),
        "anomaly_f1": result.anomaly_confusion.f1,
        "po_match": result.po_match,
        "supplier_match": result.supplier_match,
        "cost_usd": result.cost_usd,
        "latency_s": result.latency_s,
    }


def run_experiment(
    client: Langfuse,
    cases: Sequence[EvalCase],
    evaluate_case: Callable[[EvalCase], CaseResult],
    *,
    suite: str,
    model: str,
    prompt: str,
    summarize: Callable[[Sequence[CaseResult]], Summary],
) -> list[CaseResult]:
    """Run ``evaluate_case`` on each case as one experiment and return the results in order."""
    sync_dataset(client, cases)
    dataset = client.get_dataset(DATASET_NAME)
    wanted = {item_id(case.case_id) for case in cases}
    items = [item for item in dataset.items if item.id in wanted]
    by_id = {case.case_id: case for case in cases}
    collected: dict[str, CaseResult] = {}

    def task(*, item: Any, **_kwargs: Any) -> dict[str, Any]:
        case = by_id[item.input["case_id"]]
        result = evaluate_case(case)
        collected[case.case_id] = result
        return _output(result)

    def per_case(*, output: dict[str, Any], **_kwargs: Any) -> list[Evaluation]:
        return [
            Evaluation(name="field_accuracy", value=float(output["field_accuracy"])),
            Evaluation(name="extraction_accepted", value=output["status"] == "ok"),
            Evaluation(name="anomaly_f1", value=float(output["anomaly_f1"])),
            Evaluation(name="cost_usd", value=float(output["cost_usd"])),
        ]

    def aggregate(*, item_results: Any = None, **_kwargs: Any) -> list[Evaluation]:
        summary = summarize([collected[c.case_id] for c in cases if c.case_id in collected])
        anomaly = summary.anomaly
        return [
            Evaluation(name="field_accuracy_micro", value=summary.field_accuracy),
            Evaluation(name="extraction_rate", value=summary.extraction_rate),
            Evaluation(name="anomaly_precision", value=anomaly.precision),
            Evaluation(name="anomaly_recall", value=anomaly.recall),
            Evaluation(name="po_accuracy", value=summary.po_accuracy),
            Evaluation(name="injection_resisted", value=summary.injection_resisted),
            Evaluation(name="cost_total_usd", value=float(summary.cost_usd)),
            Evaluation(name="latency_p95_s", value=summary.latency_p95),
        ]

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    client.run_experiment(
        name=f"{suite} suite",
        run_name=f"{model} | {prompt} | {stamp}",
        description=f"Extraction and rules on the {suite} suite",
        data=items,
        task=task,
        evaluators=[per_case],
        run_evaluators=[aggregate],
        max_concurrency=1,  # sequential: the cost cap and the latencies stay meaningful
        metadata={"model": model, "prompt": prompt, "suite": suite},
    )
    client.flush()
    return [collected[case.case_id] for case in cases if case.case_id in collected]
