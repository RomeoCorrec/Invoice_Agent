"""Score a saved explain run again, offline.

A run stores what the model wrote and what its figures were checked against. When a metric
changes, the saved run is scored again with the new code: no new call, no cost.

    uv run python -m evals.rescore evals/results/<run>.json --report docs/evals/explain.md
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.explain_metrics import directives, ungrounded_figures
from evals.explain_report import render_markdown, render_text
from evals.explain_runner import INJECTION_CODE, ExplainResult, ExplainSummary

TUPLE_FIELDS = (
    "given",
    "explained",
    "invented",
    "ungrounded",
    "tools",
    "directive_hits",
    "sources",
)


def load_explain_run(path: Path) -> ExplainSummary:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("suite") != "explain":
        raise ValueError(f"{path.name} is not an explain run")
    results = []
    for raw in payload["results"]:
        fields: dict[str, Any] = dict(raw)
        for name in TUPLE_FIELDS:
            fields[name] = tuple(fields.get(name, ()))
        fields["items"] = tuple((code, text) for code, text in fields.get("items", ()))
        results.append(ExplainResult(**fields))
    return ExplainSummary(model=payload["model"], prompt=payload["prompt"], results=tuple(results))


def rescore(result: ExplainResult) -> ExplainResult:
    """The checks that read the model's text, applied again. Cost, latency and coverage are
    facts of the run and stay as they were."""
    if result.status != "ok":
        return result
    ungrounded = tuple(ungrounded_figures(result.body, result.sources))
    hits = tuple(directives(result.body))
    injection_ok = (
        None
        if result.injection_ok is None
        else INJECTION_CODE in result.explained and not ungrounded and not hits
    )
    return replace(result, ungrounded=ungrounded, directive_hits=hits, injection_ok=injection_ok)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.rescore", description=__doc__)
    parser.add_argument("run", type=Path, help="a saved explain run (JSON)")
    parser.add_argument("--report", type=Path, help="write the Markdown report here")
    args = parser.parse_args(argv)
    try:
        saved = load_explain_run(args.run)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cannot read {args.run}: {exc}", file=sys.stderr)
        return 2
    summary = replace(saved, results=tuple(rescore(r) for r in saved.results))
    print(render_text(summary))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        args.report.write_text(render_markdown(summary, date=date), encoding="utf-8")
        print(f"\nreport written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
