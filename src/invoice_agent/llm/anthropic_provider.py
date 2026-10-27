"""Claude through the official Anthropic SDK.

``generate`` is one call, no tools, the reply constrained to a JSON schema and validated by
Pydantic (after the stop reason is checked). ``converse`` is a bounded conversation in which the
model may call tools before it answers with that same schema. There are no sampling parameters
(removed in the 2.x API). The SDK already retries connection errors, 408/409/429 and 5xx with
backoff; what it gives up on is reported as a transient error so the worker can try again later
with its own backoff.

Error messages built here contain a status code or an error type, never the text of the
exception: the document being processed must not leak into logs or the job table.
"""

import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import anthropic
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel, ValidationError

from invoice_agent.config import Effort
from invoice_agent.llm.pricing import cost_usd, price_for
from invoice_agent.llm.provider import (
    LlmPermanentError,
    LlmResult,
    LlmTransientError,
    LlmUsage,
)
from invoice_agent.llm.tools import ToolCall, ToolSpec, run_tool

TRANSIENT_STATUS = frozenset({408, 409, 429, 529})
BUDGET_EXHAUSTED = "tool budget exhausted"


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        effort: Effort = "low",
        client: anthropic.Anthropic | None = None,
    ) -> None:
        price_for(model)  # fail at start-up, not on the first invoice, if the model is unpriced
        self._model = model
        self._effort = effort
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_s,
            max_retries=max_retries,
            # The SDK traces its own calls through the global OpenTelemetry provider, which
            # Langfuse registers: each call would show up twice, with its cost counted twice.
            # TracedProvider records the call once; this provider's spans go nowhere, and no
            # trace context is sent to the API.
            open_telemetry={"tracer_provider": TracerProvider(), "propagation": False},
        )

    @property
    def model(self) -> str:
        return self._model

    def generate[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, name: str
    ) -> LlmResult[T]:
        started = time.monotonic()
        message = self._create(
            system=system,
            messages=[{"role": "user", "content": user}],
            schema=schema,
            max_tokens=max_tokens,
        )
        usage = self._usage(message)
        return LlmResult(
            parsed=self._parse(message, schema),
            usage=usage,
            cost_usd=cost_usd(self._model, usage),
            model=self._model,
            latency_s=time.monotonic() - started,
            request_id=getattr(message, "_request_id", None),
            stop_reason=message.stop_reason or "unknown",
        )

    def converse[T: BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        name: str,
        max_tool_calls: int = 4,
        max_turns: int = 5,
    ) -> LlmResult[T]:
        started = time.monotonic()
        by_name = {tool.name: tool for tool in tools}
        tool_params = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        messages: list[Any] = [{"role": "user", "content": user}]
        calls: list[ToolCall] = []
        usage, cost, request_id = LlmUsage(), Decimal(0), None

        for turn in range(max_turns):
            # On the last turn, or once the tool budget is spent, the model must answer.
            must_answer = turn == max_turns - 1 or len(calls) >= max_tool_calls
            message = self._create(
                system=system,
                messages=messages,
                schema=schema,
                max_tokens=max_tokens,
                tools=tool_params,
                tool_choice={"type": "none"} if must_answer else None,
            )
            turn_usage = self._usage(message)
            usage = _add(usage, turn_usage)
            cost += cost_usd(self._model, turn_usage)
            request_id = getattr(message, "_request_id", None) or request_id

            if message.stop_reason != "tool_use":
                return LlmResult(
                    parsed=self._parse(message, schema),
                    usage=usage,
                    cost_usd=cost,
                    model=self._model,
                    latency_s=time.monotonic() - started,
                    request_id=request_id,
                    stop_reason=message.stop_reason or "unknown",
                    tool_calls=tuple(calls),
                )
            if must_answer:
                raise LlmPermanentError("the model kept calling tools after being told to answer")

            requested = [block for block in message.content if block.type == "tool_use"]
            results = []
            for block in requested:
                arguments = dict(block.input) if isinstance(block.input, dict) else {}
                if len(calls) >= max_tool_calls:
                    text = BUDGET_EXHAUSTED
                    call = ToolCall(block.name, arguments, ok=False, error=BUDGET_EXHAUSTED)
                else:
                    text, call = run_tool(by_name, block.name, arguments)
                calls.append(call)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": not call.ok,
                    }
                )
            messages.append({"role": "assistant", "content": _echo(message)})
            messages.append({"role": "user", "content": results})
        raise LlmPermanentError("the conversation did not finish within the allowed turns")

    # --- one request ---------------------------------------------------------------------

    def _create(
        self,
        *,
        system: str,
        messages: list[Any],
        schema: type[BaseModel],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, str] | None = None,
    ) -> anthropic.types.Message:
        arguments: dict[str, Any] = {}
        if tools:
            arguments["tools"] = tools
        if tool_choice is not None:
            arguments["tool_choice"] = tool_choice
        try:
            # create(), not parse(): parse() validates the text before we can look at the stop
            # reason, so a refusal (empty content) would be reported as a schema mismatch.
            message: anthropic.types.Message = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={
                    "effort": self._effort,
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(schema.model_json_schema()),
                    },
                },
                **arguments,
            )
            return message
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if status >= 500 or status in TRANSIENT_STATUS:
                raise LlmTransientError(f"model provider unavailable (HTTP {status})") from exc
            raise LlmPermanentError(f"model request rejected (HTTP {status})") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LlmTransientError(f"model provider unreachable ({type(exc).__name__})") from exc

    @staticmethod
    def _parse[T: BaseModel](message: anthropic.types.Message, schema: type[T]) -> T:
        stop_reason = message.stop_reason or "unknown"
        if stop_reason == "refusal":
            raise LlmPermanentError("the model declined to process this document")
        if stop_reason == "max_tokens":
            raise LlmPermanentError("the model reply was cut off (max_tokens)")
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise LlmPermanentError("the model reply does not match the expected schema") from exc

    @staticmethod
    def _usage(message: anthropic.types.Message) -> LlmUsage:
        return LlmUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )


def _add(first: LlmUsage, second: LlmUsage) -> LlmUsage:
    return LlmUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
        cache_write_tokens=first.cache_write_tokens + second.cache_write_tokens,
    )


def _echo(message: anthropic.types.Message) -> list[dict[str, Any]]:
    """The assistant's turn as the API wants it back: its text and its tool requests."""
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return blocks
