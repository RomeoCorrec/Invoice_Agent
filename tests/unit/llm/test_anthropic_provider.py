import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from invoice_agent.llm.anthropic_provider import AnthropicProvider
from invoice_agent.llm.pricing import UnknownModelError
from invoice_agent.llm.provider import LlmPermanentError, LlmTransientError

MODEL = "claude-haiku-5-5"


class Answer(BaseModel):
    total: str


def message(text: str = '{"total": "12.50"}', **overrides: Any) -> dict[str, Any]:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1800,
            "output_tokens": 300,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    return {**body, **overrides}


def provider(
    handler: Callable[[httpx2.Request], httpx2.Response], max_retries: int = 0
) -> AnthropicProvider:
    client = anthropic.Anthropic(
        api_key="sk-ant-test-key",
        max_retries=max_retries,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider(api_key="unused", model=MODEL, client=client)


def ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=message(), headers={"request-id": "req_abc"})


def call(llm: AnthropicProvider) -> Answer:
    result = llm.generate(
        system="extract", user="INVOICE TEXT", schema=Answer, max_tokens=1000, name="extract"
    )
    return result.parsed


def test_a_successful_call_returns_the_parsed_reply_with_usage_and_cost() -> None:
    result = provider(ok).generate(
        system="extract", user="INVOICE TEXT", schema=Answer, max_tokens=1000, name="extract"
    )
    assert result.parsed == Answer(total="12.50")
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1800, 300)
    assert result.cost_usd == Decimal("0.00033")
    assert (result.model, result.stop_reason, result.request_id) == (MODEL, "end_turn", "req_abc")
    assert result.latency_s >= 0


def test_the_request_has_the_expected_shape() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return ok(request)

    call(provider(handler))
    [request] = seen
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["system"] == "extract"
    assert body["messages"] == [{"role": "user", "content": "INVOICE TEXT"}]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "total" in json.dumps(body["output_config"]["format"]["schema"])
    for removed in ("temperature", "top_p", "top_k", "tools", "tool_choice"):
        assert removed not in body  # sampling parameters are gone, and no tool is declared
    assert request.headers["authorization"] == "Bearer sk-ant-test-key"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_overload_and_server_errors_are_transient(status: int) -> None:
    llm = provider(
        lambda _r: httpx2.Response(status, json={"type": "error", "error": {"type": "x"}})
    )
    with pytest.raises(LlmTransientError, match=str(status)):
        call(llm)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_client_errors_are_permanent(status: int) -> None:
    llm = provider(
        lambda _r: httpx2.Response(status, json={"type": "error", "error": {"type": "x"}})
    )
    with pytest.raises(LlmPermanentError, match=str(status)):
        call(llm)


def test_network_failures_are_transient() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LlmTransientError, match="unreachable"):
        call(provider(handler))


def test_timeouts_are_transient() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(LlmTransientError):
        call(provider(handler))


def test_the_sdk_retries_before_giving_up() -> None:
    attempts: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx2.Response(429, json={"type": "error"}, headers={"retry-after-ms": "1"})
        return ok(request)

    assert call(provider(handler, max_retries=2)) == Answer(total="12.50")
    assert len(attempts) == 3


def test_a_refusal_is_permanent_and_not_retried() -> None:
    llm = provider(lambda _r: httpx2.Response(200, json=message("", stop_reason="refusal")))
    with pytest.raises(LlmPermanentError, match="declined"):
        call(llm)


def test_a_reply_cut_off_by_max_tokens_is_permanent() -> None:
    llm = provider(
        lambda _r: httpx2.Response(200, json=message('{"total": "12', stop_reason="max_tokens"))
    )
    with pytest.raises(LlmPermanentError, match="cut off"):
        call(llm)


def test_a_reply_that_does_not_match_the_schema_is_permanent() -> None:
    llm = provider(lambda _r: httpx2.Response(200, json=message('{"unexpected": 1}')))
    with pytest.raises(LlmPermanentError):
        call(llm)


def test_errors_never_echo_what_the_api_said_or_the_document() -> None:
    secret = "FR7699053965263585803695442 and the invoice text"
    llm = provider(
        lambda _r: httpx2.Response(400, json={"type": "error", "error": {"message": secret}})
    )
    with pytest.raises(LlmPermanentError) as error:
        call(llm)
    assert "FR76" not in str(error.value)
    assert "invoice text" not in str(error.value)


def test_an_unpriced_model_fails_at_start_up() -> None:
    with pytest.raises(UnknownModelError):
        AnthropicProvider(api_key="k", model="claude-unknown-1")
