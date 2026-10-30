"""Human-readable reports of a query (supplier message) evaluation."""

from evals.query_runner import QuerySummary


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def metrics(summary: QuerySummary) -> list[tuple[str, str]]:
    return [
        ("Drafts made", str(len(summary.drafted))),
        ("Model reachable", _pct(summary.available)),
        ("Model draft passed every check", _pct(summary.accepted)),
        ("No directive to the reader", _pct(summary.directive_free)),
        ("Cost (USD)", f"{summary.cost_usd:.4f}"),
        ("Latency p50 / p95 (s)", f"{summary.latency_p50:.2f} / {summary.latency_p95:.2f}"),
    ]


def render_text(summary: QuerySummary) -> str:
    lines = [f"suite=query model={summary.model} prompt={summary.prompt}", ""]
    width = max(len(name) for name, _ in metrics(summary))
    lines += [f"{name:<{width}}  {value}" for name, value in metrics(summary)]
    lines += ["", f"{'case':<8} {'kind':<9} {'status':<12} {'source':<9} {'secs':>6}  notes"]
    for r in summary.results:
        notes = r.reason or "; ".join(
            [*([r.fallback_reason] if r.fallback_reason else []), *r.problems, *r.directive_hits]
        )
        kind = "reminder" if r.reminder else "first"
        lines.append(
            f"{r.case_id:<8} {kind:<9} {r.status:<12} {r.source:<9} {r.latency_s:6.2f}  {notes}"
        )
    return "\n".join(lines)


def render_markdown(summary: QuerySummary, *, date: str) -> str:
    lines = [
        "# Evaluation: query suite",
        "",
        f"- Date: {date}",
        f"- Model: `{summary.model}`",
        f"- Prompt: `{summary.prompt}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {name} | {value} |" for name, value in metrics(summary)],
        "",
        "| Case | Message | Status | Source | Why not the model's | Latency (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in summary.results:
        why = "; ".join([*([r.fallback_reason] if r.fallback_reason else []), *r.problems])
        lines.append(
            f"| {r.case_id} | {'reminder' if r.reminder else 'first'} | {r.status} | "
            f"{r.source or '-'} | {why or '-'} | {r.latency_s:.2f} |"
        )
    written = [r for r in summary.drafted if r.source == "model"]
    if written:
        lines += ["", "## What the model wrote (first messages)", ""]
        for r in written:
            if not r.reminder:
                lines += [f"### {r.case_id}", "", f"**{r.subject}**", "", r.body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"
