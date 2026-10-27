"""A provider wrapper that records every call as a Langfuse generation."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

from pydantic import BaseModel

from invoice_agent.llm.provider import LlmError, LlmProvider, LlmResult, ToolProvider
from invoice_agent.llm.tools import ToolSpec
from invoice_agent.observability.tracing import Tracing


class TracedProvider:
    """Decorates any ``LlmProvider``: one generation per call, with tokens, cost and latency.

    The system prompt is not sent (it is a file in the repository); its label, which carries
    the version and a hash, goes in the metadata instead.
    """

    def __init__(self, inner: LlmProvider, tracing: Tracing, *, prompt_label: str | None = None):
        self._inner = inner
        self._tracing = tracing
        self._prompt_label = prompt_label

    @property
    def model(self) -> str:
        return self._inner.model

    def generate[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, name: str
    ) -> LlmResult[T]:
        return self._traced(
            name,
            user,
            max_tokens,
            lambda: self._inner.generate(
                system=system, user=user, schema=schema, max_tokens=max_tokens, name=name
            ),
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
        """A conversation with tools: one generation for the whole exchange (its usage and
        cost cover every request) and one ``tool`` span per tool call, inside it."""
        if not hasattr(self._inner, "converse"):
            raise TypeError("the wrapped provider cannot run tools")
        inner = cast(ToolProvider, self._inner)
        traced_tools = [self._trace_tool(tool) for tool in tools]
        return self._traced(
            name,
            user,
            max_tokens,
            lambda: inner.converse(
                system=system,
                user=user,
                schema=schema,
                tools=traced_tools,
                max_tokens=max_tokens,
                name=name,
                max_tool_calls=max_tool_calls,
                max_turns=max_turns,
            ),
        )

    def _trace_tool(self, tool: ToolSpec) -> ToolSpec:
        def handler(arguments: dict[str, Any]) -> str:
            with self._tracing.observation(
                f"tool.{tool.name}", as_type="tool", input=arguments
            ) as span:
                try:
                    output = tool.handler(arguments)
                except Exception as exc:
                    span.update(level="ERROR", status_message=type(exc).__name__)
                    raise
                span.update(output=output)
                return output

        return replace(tool, handler=handler)

    def _traced[T: BaseModel](
        self, name: str, user: str, max_tokens: int, call: Any
    ) -> LlmResult[T]:
        with self._tracing.observation(
            name,
            as_type="generation",
            model=self._inner.model,
            input=user,
            metadata={"prompt": self._prompt_label, "max_tokens": max_tokens},
        ) as observation:
            try:
                result: LlmResult[T] = call()
            except LlmError as exc:
                observation.update(level="ERROR", status_message=str(exc))
                raise
            usage = result.usage
            observation.update(
                output=result.parsed.model_dump(mode="json"),
                usage_details={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cache_read_input_tokens": usage.cache_read_tokens,
                    "cache_creation_input_tokens": usage.cache_write_tokens,
                },
                cost_details={"total": float(result.cost_usd)},
                metadata={
                    "prompt": self._prompt_label,
                    "request_id": result.request_id,
                    "stop_reason": result.stop_reason,
                    "latency_s": round(result.latency_s, 3),
                    "tool_calls": [call.name for call in result.tool_calls],
                },
            )
            return result
