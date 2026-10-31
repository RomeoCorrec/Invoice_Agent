"""The thresholds of the query suite apply to its measured values."""

from evals.query_runner import QueryResult, QuerySummary
from evals.thresholds import check, check_query, load_thresholds, query_values


def draft(source: str = "model", **changes: object) -> QueryResult:
    base = QueryResult("INV-12", False, "ok", source=source, cost_usd=0.0004, latency_s=2.0)
    return QueryResult(**{**base.__dict__, **changes})  # type: ignore[arg-type]


def summary(*results: QueryResult) -> QuerySummary:
    return QuerySummary(model="m", prompt="p", results=results)


def test_the_committed_limits_talk_about_metrics_that_exist() -> None:
    values = query_values(summary(draft()))
    assert check(values, load_thresholds()["query"]) == []


def test_good_drafts_meet_every_limit() -> None:
    assert check_query(summary(draft(), draft(reminder=True))) == []


def test_too_many_refused_drafts_break_the_acceptance_rate() -> None:
    refused = draft("template", fallback_reason="draft refused", problems=("x",))
    names = {v.name for v in check_query(summary(draft(), refused, refused))}
    assert names == {"accepted"}


def test_an_unreachable_model_breaks_availability_too() -> None:
    down = draft("template", fallback_reason="model unavailable")
    names = {v.name for v in check_query(summary(down, down, down))}
    assert {"available", "accepted"} <= names


def test_a_directive_to_the_reader_is_never_tolerated() -> None:
    bad = draft(directive_hits=("approuvez",))
    assert [v.name for v in check_query(summary(*[draft()] * 30, bad))] == ["directive_free"]


def test_cost_and_latency_are_upper_bounds() -> None:
    slow = draft(cost_usd=0.05, latency_s=30.0)
    names = {(v.name, v.kind) for v in check_query(summary(draft(), slow))}
    assert names == {("cost_per_draft_usd", "max"), ("latency_p95_s", "max")}
