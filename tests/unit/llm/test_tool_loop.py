"""The bounded tool conversation of the Anthropic provider, against a scripted HTTP transport."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from invoice_agent.llm.anthropic_provider import BUDGET_EXHAUSTED, AnthropicProvider
from invoice_agent.llm.provider import LlmPermanentError, LlmTransientError
from invoice_agent.llm.tools import MAX_TOOL_RESULT_CHARS, ToolFailure, ToolSpec
from tests.unit.llm.test_anthropic_provider import Answer, message, provider


def tool_use(*calls: tuple[str, str, dict[str, Any]], **usage: int) -> dict[str, Any]:
    """A reply in which the model asks for tools."""
    body = message(
        stop_reason="tool_use",
        content=[
            {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
            for call_id, name, arguments in calls
        ],
    )
    body["usage"] = {**body["usage"], **usage}
    return body


def final(text: str = '{"total": "12.50"}') -> dict[str, Any]:
    return message(text)


class Script:
    """Replies in order, and keeps the requests it received."""

    def __init__(self, *replies: dict[str, Any] | int) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        reply = self._replies.pop(0)
        if isinstance(reply, int):
            return httpx2.Response(
                reply, json={"type": "error", "error": {"type": "x", "message": "y"}}
            )
        return httpx2.Response(200, json=reply)


def lookup_tool(handler: Callable[[dict[str, Any]], str] | None = None) -> ToolSpec:
    return ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler or (lambda args: json.dumps({"found": args.get("key")})),
    )


def converse(script: Script, *tools: ToolSpec, **limits: int) -> Any:
    llm: AnthropicProvider = provider(script)
    return llm.converse(
        system="explain",
        user="WHY",
        schema=Answer,
        tools=tools or (lookup_tool(),),
        max_tokens=1000,
        name="explain",
        **limits,
    )


def test_a_tool_call_then_an_answer() -> None:
    script = Script(tool_use(("tu_1", "lookup", {"key": "A"}), input_tokens=100), final())
    result = converse(script)

    assert result.parsed.total == "12.50"
    assert [(c.name, c.arguments, c.ok) for c in result.tool_calls] == [
        ("lookup", {"key": "A"}, True)
    ]
    first, second = script.requests
    assert [t["name"] for t in first["tools"]] == ["lookup"]
    assert "tool_choice" not in first
    assert first["output_config"]["format"]["type"] == "json_schema"
    # the second request echoes the model's request and answers it with the tool's result
    assert second["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {"key": "A"}}],
    }
    [tool_result] = second["messages"][2]["content"]
    assert tool_result["tool_use_id"] == "tu_1" and tool_result["is_error"] is False
    assert json.loads(tool_result["content"]) == {"found": "A"}


def test_usage_and_cost_cover_every_request() -> None:
    script = Script(tool_use(("tu_1", "lookup", {}), input_tokens=100), final())
    result = converse(script)
    assert result.usage.input_tokens == 100 + 1800 and result.usage.output_tokens == 600
    one = Decimal("0.1") * 100 / 1_000_000 + Decimal("0.5") * 300 / 1_000_000
    two = Decimal("0.1") * 1800 / 1_000_000 + Decimal("0.5") * 300 / 1_000_000
    assert result.cost_usd == one + two


def test_the_tool_budget_is_enforced_and_the_model_is_then_told_to_answer() -> None:
    calls = [
        ("a", "lookup", {"key": "1"}),
        ("b", "lookup", {"key": "2"}),
        ("c", "lookup", {"key": "3"}),
    ]
    script = Script(tool_use(*calls), final())
    result = converse(script, max_tool_calls=2)

    assert [c.ok for c in result.tool_calls] == [True, True, False]
    assert result.tool_calls[2].error == BUDGET_EXHAUSTED
    results = script.requests[1]["messages"][2]["content"]
    assert [r["is_error"] for r in results] == [False, False, True]
    assert script.requests[1]["tool_choice"] == {"type": "none"}  # budget spent: answer now


def test_the_last_turn_forbids_tools_and_a_model_that_insists_is_stopped() -> None:
    script = Script(
        tool_use(("a", "lookup", {})), tool_use(("b", "lookup", {})), tool_use(("c", "lookup", {}))
    )
    with pytest.raises(LlmPermanentError, match="after being told to answer"):
        converse(script, max_turns=3, max_tool_calls=10)
    assert [r.get("tool_choice") for r in script.requests] == [None, None, {"type": "none"}]


def test_an_unknown_tool_is_reported_to_the_model_not_run() -> None:
    script = Script(tool_use(("a", "delete_everything", {})), final())
    result = converse(script)
    assert result.tool_calls[0].ok is False and result.tool_calls[0].error == "unknown tool"
    [tool_result] = script.requests[1]["messages"][2]["content"]
    assert tool_result["is_error"] is True and tool_result["content"] == "unknown tool"


def test_a_tool_failure_message_reaches_the_model_but_a_crash_does_not_leak() -> None:
    def refuses(_args: dict[str, Any]) -> str:
        raise ToolFailure("no such supplier")

    def crashes(_args: dict[str, Any]) -> str:
        raise RuntimeError("password=hunter2 host=db.internal")

    refusing, crashing = lookup_tool(refuses), lookup_tool(crashes)
    refusing_script = Script(tool_use(("a", "lookup", {})), final())
    result = converse(refusing_script, refusing)
    assert refusing_script.requests[1]["messages"][2]["content"][0]["content"] == "no such supplier"
    assert result.tool_calls[0].error == "no such supplier"

    crashing_script = Script(tool_use(("a", "lookup", {})), final())
    result = converse(crashing_script, crashing)
    sent = json.dumps(crashing_script.requests[1])
    assert "hunter2" not in sent and "db.internal" not in sent
    assert result.tool_calls[0].error == "the tool failed"


def test_a_long_tool_result_is_cut_before_it_goes_back_to_the_model() -> None:
    script = Script(tool_use(("a", "lookup", {})), final())
    converse(script, lookup_tool(lambda _a: "x" * (MAX_TOOL_RESULT_CHARS * 3)))
    sent = script.requests[1]["messages"][2]["content"][0]["content"]
    assert len(sent) == MAX_TOOL_RESULT_CHARS


def test_the_conversation_fails_like_a_single_call_when_the_provider_does() -> None:
    with pytest.raises(LlmTransientError, match="529"):
        converse(Script(tool_use(("a", "lookup", {})), 529))
    with pytest.raises(LlmPermanentError, match="400"):
        converse(Script(tool_use(("a", "lookup", {})), 400))


def test_a_refusal_or_a_malformed_final_answer_is_a_permanent_error() -> None:
    with pytest.raises(LlmPermanentError, match="declined"):
        converse(Script(tool_use(("a", "lookup", {})), message("", stop_reason="refusal")))
    with pytest.raises(LlmPermanentError, match="schema"):
        converse(Script(tool_use(("a", "lookup", {})), final("not json")))


def test_a_model_that_needs_no_tool_answers_in_one_request() -> None:
    script = Script(final())
    result = converse(script)
    assert result.tool_calls == () and len(script.requests) == 1
