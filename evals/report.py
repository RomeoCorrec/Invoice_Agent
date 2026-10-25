"""Human-readable reports of an evaluation run."""

from evals.runner import Summary


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def metrics(summary: Summary) -> list[tuple[str, str]]:
    anomaly = summary.anomaly
    return [
        ("Cases scored", f"{len(summary.scored)} / {len(summary.results)}"),
        ("Extraction accepted (grounded)", _pct(summary.extraction_rate)),
        ("Field accuracy (micro)", _pct(summary.field_accuracy)),
        ("Exact-match cases", _pct(summary.exact_match_rate)),
        ("Anomaly precision", _pct(anomaly.precision)),
        ("Anomaly recall", _pct(anomaly.recall)),
        ("Anomaly F1", _pct(anomaly.f1)),
        ("Purchase-order match", _pct(summary.po_accuracy)),
        ("Supplier match", _pct(summary.supplier_accuracy)),
        ("Injection: figures unchanged", _pct(summary.injection_resisted)),
        ("Injection: flagged for review", _pct(summary.injection_flagged)),
        ("Injection: blocked (failed closed)", _pct(summary.injection_blocked)),
        ("Cost (USD)", f"{summary.cost_usd:.4f}"),
        ("Latency p50 / p95 (s)", f"{summary.latency_p50:.2f} / {summary.latency_p95:.2f}"),
    ]


def render_text(summary: Summary) -> str:
    lines = [f"suite={summary.suite} model={summary.model} prompt={summary.prompt}", ""]
    width = max(len(name) for name, _ in metrics(summary))
    lines += [f"{name:<{width}}  {value}" for name, value in metrics(summary)]
    lines += ["", f"{'case':<8} {'status':<9} {'fields':>7} {'cost $':>9} {'secs':>6}  notes"]
    for r in summary.results:
        notes = r.reason or ""
        wrong = [name for name, ok in r.fields.items() if not ok]
        if r.status == "ok" and wrong:
            notes = "wrong: " + ", ".join(wrong[:4]) + ("..." if len(wrong) > 4 else "")
        lines.append(
            f"{r.case_id:<8} {r.status:<9} {r.field_accuracy * 100:6.1f}% "
            f"{r.cost_usd:9.5f} {r.latency_s:6.2f}  {notes}"
        )
    return "\n".join(lines)


def render_markdown(summary: Summary, *, date: str) -> str:
    lines = [
        f"# Evaluation: {summary.suite} suite",
        "",
        f"- Date: {date}",
        f"- Model: `{summary.model}`",
        f"- Prompt: `{summary.prompt}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {name} | {value} |" for name, value in metrics(summary)],
        "",
        "| Case | Status | Field accuracy | Anomalies found | Cost (USD) | Latency (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in summary.results:
        found = ", ".join(sorted(r.anomalies)) or "-"
        lines.append(
            f"| {r.case_id} | {r.status} | {r.field_accuracy * 100:.1f}% | {found} | "
            f"{r.cost_usd:.5f} | {r.latency_s:.2f} |"
        )
    return "\n".join(lines) + "\n"
