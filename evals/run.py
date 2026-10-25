"""Command line of the evaluations.

    uv run --env-file .env python -m evals.run --suite smoke --dry-run
    uv run --env-file .env python -m evals.run --suite smoke --yes
    uv run --env-file .env python -m evals.run --suite full --yes --report docs/evals/latest.md

Calling the API costs money, so nothing is sent without ``--yes``; ``--max-cost`` stops the
run once the cap is reached. Results go to Langfuse when it is configured (as an experiment on
the golden dataset) and always to ``evals/results/`` (ignored by git).
"""

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.context import load_reference
from evals.dataset import EvalCase, load_cases, select
from evals.langfuse_run import run_experiment
from evals.report import render_markdown, render_text
from evals.runner import (
    CaseResult,
    CostBudget,
    RecordingProvider,
    Summary,
    run_case,
    run_suite,
    skipped,
)
from invoice_agent.config import ConfigError, load_settings
from invoice_agent.llm.anthropic_provider import AnthropicProvider
from invoice_agent.llm.extractor import LlmInvoiceExtractor
from invoice_agent.llm.pricing import cost_usd
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.llm.provider import LlmUsage
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing

RESULTS_DIR = Path(__file__).parent / "results"
# A generous guess of one call's size, to size the worst case before spending anything.
WORST_CASE_USAGE = LlmUsage(input_tokens=4000, output_tokens=3000)


def worst_case_cost(model: str, cases: int) -> Decimal:
    return cost_usd(model, WORST_CASE_USAGE) * cases


def _json_default(value: Any) -> Any:
    if isinstance(value, frozenset | set):
        return sorted(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def save_results(summary: Summary, directory: Path | None = None) -> Path:
    directory = directory or RESULTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{summary.suite}-{summary.model}.json"
    payload = {
        "suite": summary.suite,
        "model": summary.model,
        "prompt": summary.prompt,
        "results": [dataclasses.asdict(result) for result in summary.results],
    }
    path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    parser.add_argument("--suite", choices=["smoke", "full"], required=True)
    parser.add_argument("--model", help="defaults to LLM_MODEL (claude-haiku-5-5)")
    parser.add_argument("--max-cost", type=float, default=2.0, help="USD cap for the run")
    parser.add_argument("--yes", action="store_true", help="actually call the API (it costs money)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    parser.add_argument(
        "--no-langfuse", action="store_true", help="do not send anything to Langfuse"
    )
    parser.add_argument("--report", type=Path, help="also write a Markdown report here")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    model = args.model or settings.llm.model
    cases = select(load_cases(settings.iban_hash_key), args.suite)
    estimate = worst_case_cost(model, len(cases))
    print(f"{args.suite} suite: {len(cases)} cases, model {model}")
    print(f"worst case about ${estimate:.4f}, cap ${args.max_cost:.2f}")
    if args.dry_run:
        return 0
    if not args.yes:
        print("refusing to call the API without --yes (this spends money)", file=sys.stderr)
        return 2
    if settings.llm.api_key is None:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    tracing = Tracing() if args.no_langfuse else Tracing.from_env(os.environ)
    inner = AnthropicProvider(
        api_key=settings.llm.api_key,
        model=model,
        timeout_s=settings.llm.timeout_s,
        max_retries=settings.llm.max_retries,
        effort=settings.llm.effort,
    )
    recorder = RecordingProvider(inner)
    prompt_file = load_prompt("extract_invoice", 1)
    extractor = LlmInvoiceExtractor(
        TracedProvider(recorder, tracing, prompt_label=prompt_file.label),
        settings.iban_hash_key,
        prompt=prompt_file,
    )
    prompt = prompt_file.label
    reference = load_reference(settings.iban_hash_key)
    budget = CostBudget(args.max_cost)

    def evaluate(case: EvalCase) -> CaseResult:
        if budget.exhausted:
            return skipped(case)
        result = run_case(case, extractor, recorder, reference, settings.iban_hash_key)
        budget.charge(result.cost_usd)
        return result

    def summarize(results: Sequence[CaseResult]) -> Summary:
        return Summary(suite=args.suite, model=model, prompt=prompt, results=tuple(results))

    if tracing.client is not None:
        results = run_experiment(
            tracing.client,
            cases,
            evaluate,
            suite=args.suite,
            model=model,
            prompt=prompt,
            summarize=summarize,
        )
        summary = summarize(results)
    else:
        summary = run_suite(
            cases,
            extractor,
            recorder,
            reference,
            settings.iban_hash_key,
            budget,
            suite=args.suite,
            prompt=prompt,
        )
    tracing.shutdown()

    print()
    print(render_text(summary))
    print(f"\nresults saved to {save_results(summary)}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        args.report.write_text(render_markdown(summary, date=date), encoding="utf-8")
        print(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
