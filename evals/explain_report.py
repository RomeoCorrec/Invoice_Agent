"""Human-readable reports of an explain evaluation."""

from evals.explain_runner import ExplainSummary


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def metrics(summary: ExplainSummary) -> list[tuple[str, str]]:
    return [
        ("Cases answered / attempted", f"{len(summary.answered)} / {len(summary.attempted)}"),
        ("Availability", _pct(summary.availability)),
        ("Anomaly coverage", _pct(summary.coverage)),
        ("No invented code", _pct(summary.clean_of_invented_codes)),
        ("No ungrounded figure", _pct(summary.clean_of_ungrounded_figures)),
        ("Tool discipline (read-only, within budget)", _pct(summary.tool_discipline)),
        ("Injection case handled", _pct(summary.injection_ok)),
        ("No directive to the reviewer", _pct(summary.directive_free)),
        ("Tool calls per case", f"{summary.tools_per_case:.1f}"),
        ("Cost (USD)", f"{summary.cost_usd:.4f}"),
        ("Latency p50 / p95 (s)", f"{summary.latency_p50:.2f} / {summary.latency_p95:.2f}"),
    ]


def render_text(summary: ExplainSummary) -> str:
    lines = [f"suite=explain model={summary.model} prompt={summary.prompt}", ""]
    width = max(len(name) for name, _ in metrics(summary))
    lines += [f"{name:<{width}}  {value}" for name, value in metrics(summary)]
    header = (
        f"{'case':<8} {'status':<12} {'cover':>6} {'tools':>5} {'cost $':>9} {'secs':>6}  notes"
    )
    lines += ["", header]
    for r in summary.results:
        notes = r.reason or ""
        if r.status == "ok":
            found = []
            if r.invented:
                found.append("invented " + ",".join(r.invented))
            if r.ungrounded:
                found.append("ungrounded " + ",".join(r.ungrounded))
            if r.directive_hits:
                found.append("directive " + ",".join(r.directive_hits))
            if not r.discipline_ok:
                found.append("tool discipline")
            notes = "; ".join(found)
        lines.append(
            f"{r.case_id:<8} {r.status:<12} {r.coverage * 100:5.0f}% {len(r.tools):>5} "
            f"{r.cost_usd:9.5f} {r.latency_s:6.2f}  {notes}"
        )
    return "\n".join(lines)


def render_markdown(summary: ExplainSummary, *, date: str) -> str:
    lines = [
        "# Evaluation: explain suite",
        "",
        f"- Date: {date}",
        f"- Model: `{summary.model}`",
        f"- Prompt: `{summary.prompt}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {name} | {value} |" for name, value in metrics(summary)],
        "",
        "| Case | Status | Anomalies given | Explained | Tools | Cost (USD) | Latency (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in summary.results:
        lines.append(
            f"| {r.case_id} | {r.status} | {', '.join(r.given) or '-'} | "
            f"{', '.join(r.explained) or '-'} | {', '.join(r.tools) or '-'} | "
            f"{r.cost_usd:.5f} | {r.latency_s:.2f} |"
        )
    answered = [r for r in summary.results if r.status == "ok"]
    if answered:
        lines += ["", "## What the model wrote", ""]
        for r in answered:
            lines += [f"### {r.case_id}", "", r.summary, ""]
            lines += [f"- `{code}`: {text}" for code, text in r.items]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
