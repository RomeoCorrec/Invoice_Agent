"""Read ``thresholds.yaml`` and compare a run with it.

The file only uses sections of ``name: number`` lines, so it is read by a few lines of code
instead of a YAML library (adding a dependency needs the owner's agreement). Anything outside
that subset is an error rather than silently ignored: a threshold that is not applied is worse
than none.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from evals.explain_runner import ExplainSummary

THRESHOLDS_FILE = Path(__file__).parent / "thresholds.yaml"


class ThresholdsError(ValueError):
    pass


@dataclass(frozen=True)
class Violation:
    name: str
    actual: float
    limit: float
    kind: str
    """``min`` or ``max``."""

    def __str__(self) -> str:
        sign = "<" if self.kind == "min" else ">"
        return f"{self.name}: {self.actual:.4g} {sign} {self.limit:.4g}"


def load_thresholds(path: Path = THRESHOLDS_FILE) -> dict[str, dict[str, float]]:
    sections: dict[str, dict[str, float]] = {}
    current: dict[str, float] | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        where = f"{path.name} line {number}"
        if not raw.startswith((" ", "\t")):
            if not line.endswith(":") or ":" in line[:-1]:
                raise ThresholdsError(f"{where}: expected a section name ending with ':'")
            current = sections.setdefault(line[:-1].strip(), {})
            continue
        if current is None:
            raise ThresholdsError(f"{where}: a value outside any section")
        name, separator, value = line.strip().partition(":")
        if not separator or not name.startswith(("min_", "max_")):
            raise ThresholdsError(f"{where}: expected 'min_<metric>: <number>' or 'max_...'")
        try:
            current[name] = float(value)
        except ValueError:
            raise ThresholdsError(f"{where}: {value.strip()!r} is not a number") from None
    return sections


def explain_values(summary: ExplainSummary) -> dict[str, float]:
    """The measured values the thresholds of the explain suite talk about."""
    attempted = len(summary.attempted)
    return {
        "availability": summary.availability,
        "coverage": summary.coverage,
        "no_invented_code": summary.clean_of_invented_codes,
        "no_ungrounded_figure": summary.clean_of_ungrounded_figures,
        "tool_discipline": summary.tool_discipline,
        "injection_ok": summary.injection_ok,
        "cost_per_case_usd": float(summary.cost_usd) / attempted if attempted else 0.0,
    }


def check(values: dict[str, float], limits: dict[str, float]) -> list[Violation]:
    """Every limit that the values break. A limit about an unknown metric is an error."""
    violations = []
    for name, limit in limits.items():
        kind, _, metric = name.partition("_")
        if metric not in values:
            raise ThresholdsError(f"threshold {name!r} is about an unknown metric {metric!r}")
        actual = values[metric]
        if (kind == "min" and actual < limit) or (kind == "max" and actual > limit):
            violations.append(Violation(metric, actual, limit, kind))
    return violations


def check_explain(summary: ExplainSummary, path: Path = THRESHOLDS_FILE) -> list[Violation]:
    limits = load_thresholds(path).get("explain")
    if limits is None:
        raise ThresholdsError(f"{path.name} has no 'explain' section")
    return check(explain_values(summary), limits)


def report_explain(summary: ExplainSummary, path: Path = THRESHOLDS_FILE) -> int:
    """Print how the run compares with the thresholds; the exit code of the command."""
    try:
        violations = check_explain(summary, path)
    except (ThresholdsError, OSError) as exc:
        print(f"thresholds: {exc}", file=sys.stderr)
        return 2
    if violations:
        print("\nbelow the thresholds of evals/thresholds.yaml:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("\nthresholds: all met (evals/thresholds.yaml)")
    return 0
