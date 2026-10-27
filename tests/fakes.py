"""Test doubles for the LLM-backed extractor."""

from typing import Any

from invoice_agent.domain.invoice import InvoiceData


class ScriptedExtractor:
    """Returns a prepared result (or raises a prepared error) and records what it was given."""

    def __init__(self, result: InvoiceData | Exception) -> None:
        self._result = result
        self.received: list[str] = []

    def extract(self, text: str) -> InvoiceData:
        self.received.append(text)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class LookupExtractor:
    """Plays the LLM for several documents: picks the prepared result whose invoice number is
    printed in the text it receives."""

    def __init__(self, by_number: dict[str, InvoiceData]) -> None:
        self._by_number = by_number

    def extract(self, text: str) -> InvoiceData:
        from invoice_agent.domain.rules.injection import squash_text
        from invoice_agent.ingestion.document import ExtractionError

        printed = f" {squash_text(text)} "
        for number, data in self._by_number.items():
            if f" {squash_text(number)} " in printed:
                return data
        raise ExtractionError("unknown document")


class FakeProvider:
    """A model provider that replies from a script instead of calling an API.

    ``reply`` is the object to return, an exception to raise, or a function of the user
    message. Every call is recorded.
    """

    def __init__(self, reply: Any, model: str = "claude-haiku-5-5") -> None:
        self._reply = reply
        self._model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self._model

    def generate(self, *, system: str, user: str, schema: Any, max_tokens: int, name: str) -> Any:
        from decimal import Decimal

        from invoice_agent.llm.pricing import cost_usd
        from invoice_agent.llm.provider import LlmResult, LlmUsage

        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens, "name": name})
        reply = self._reply(user) if callable(self._reply) else self._reply
        if isinstance(reply, Exception):
            raise reply
        usage = LlmUsage(input_tokens=len(system + user) // 3, output_tokens=300)
        return LlmResult(
            parsed=schema.model_validate(reply.model_dump()),
            usage=usage,
            cost_usd=cost_usd(self._model, usage) if self._model else Decimal(0),
            model=self._model,
            latency_s=0.01,
            request_id="req_fake",
            stop_reason="end_turn",
        )


class FakeToolProvider(FakeProvider):
    """A provider whose model asks for the planned tools, then gives the scripted answer.

    The planned calls run through the same ``run_tool`` as the real provider, with the same
    budget, so what the tools receive and return is exercised. ``plan`` is a list of
    ``(tool name, arguments)`` or a function of the user message returning one.
    """

    def __init__(self, reply: Any, plan: Any = (), model: str = "claude-haiku-5-5") -> None:
        super().__init__(reply, model)
        self._plan = plan

    def converse(
        self,
        *,
        system: str,
        user: str,
        schema: Any,
        tools: Any,
        max_tokens: int,
        name: str,
        max_tool_calls: int = 4,
        max_turns: int = 5,
    ) -> Any:
        from dataclasses import replace

        from invoice_agent.llm.tools import ToolCall, run_tool

        by_name = {tool.name: tool for tool in tools}
        planned = self._plan(user) if callable(self._plan) else self._plan
        calls: list[ToolCall] = []
        for tool_name, arguments in planned:
            if len(calls) >= max_tool_calls:
                calls.append(
                    ToolCall(tool_name, arguments, ok=False, error="tool budget exhausted")
                )
                continue
            calls.append(run_tool(by_name, tool_name, arguments)[1])
        result = self.generate(
            system=system, user=user, schema=schema, max_tokens=max_tokens, name=name
        )
        self.calls[-1]["tools"] = [tool.name for tool in tools]
        self.calls[-1]["limits"] = (max_tool_calls, max_turns)
        return replace(result, tool_calls=tuple(calls))
