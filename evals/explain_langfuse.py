"""Run the explain evaluation as a Langfuse experiment on the golden dataset."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from langfuse import Evaluation, Langfuse

from evals.dataset import EvalCase
from evals.explain_runner import ExplainResult, ExplainSummary
from evals.langfuse_run import DATASET_NAME, item_id, sync_dataset


def run_explain_experiment(
    client: Langfuse,
    cases: Sequence[EvalCase],
    evaluate_case: Callable[[EvalCase], ExplainResult],
    *,
    model: str,
    prompt: str,
    summarize: Callable[[Sequence[ExplainResult]], ExplainSummary],
) -> list[ExplainResult]:
    """Run ``evaluate_case`` on each case as one experiment and return the results in order."""
    sync_dataset(client, cases)
    dataset = client.get_dataset(DATASET_NAME)
    wanted = {item_id(case.case_id) for case in cases}
    items = [item for item in dataset.items if item.id in wanted]
    by_id = {case.case_id: case for case in cases}
    collected: dict[str, ExplainResult] = {}

    def task(*, item: Any, **_kwargs: Any) -> dict[str, Any]:
        case = by_id[item.input["case_id"]]
        result = evaluate_case(case)
        collected[case.case_id] = result
        return {
            "status": result.status,
            "given": list(result.given),
            "explained": list(result.explained),
            "coverage": result.coverage,
            "invented": list(result.invented),
            "ungrounded": list(result.ungrounded),
            "tools": list(result.tools),
            "cost_usd": result.cost_usd,
        }

    def per_case(*, output: dict[str, Any], **_kwargs: Any) -> list[Evaluation]:
        if output["status"] != "ok":
            return [Evaluation(name="answered", value=False)]
        return [
            Evaluation(name="answered", value=True),
            Evaluation(name="coverage", value=float(output["coverage"])),
            Evaluation(name="no_invented_code", value=not output["invented"]),
            Evaluation(name="no_ungrounded_figure", value=not output["ungrounded"]),
            Evaluation(name="tool_calls", value=float(len(output["tools"]))),
            Evaluation(name="cost_usd", value=float(output["cost_usd"])),
        ]

    def aggregate(*, item_results: Any = None, **_kwargs: Any) -> list[Evaluation]:
        summary = summarize([collected[c.case_id] for c in cases if c.case_id in collected])
        return [
            Evaluation(name="availability", value=summary.availability),
            Evaluation(name="coverage", value=summary.coverage),
            Evaluation(name="no_invented_code", value=summary.clean_of_invented_codes),
            Evaluation(name="no_ungrounded_figure", value=summary.clean_of_ungrounded_figures),
            Evaluation(name="tool_discipline", value=summary.tool_discipline),
            Evaluation(name="injection_ok", value=summary.injection_ok),
            Evaluation(name="cost_total_usd", value=float(summary.cost_usd)),
            Evaluation(name="latency_p95_s", value=summary.latency_p95),
        ]

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    client.run_experiment(
        name="explain suite",
        run_name=f"{model} | {prompt} | {stamp}",
        description="Explanations of anomalies on the golden dataset",
        data=items,
        task=task,
        evaluators=[per_case],
        run_evaluators=[aggregate],
        max_concurrency=1,  # sequential: the cost cap and the latencies stay meaningful
        metadata={"model": model, "prompt": prompt, "suite": "explain"},
    )
    client.flush()
    return [collected[case.case_id] for case in cases if case.case_id in collected]
