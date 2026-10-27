"""The seam between the application and a language model provider.

Every LLM call goes through a ``LlmProvider`` so that timeouts, retries, tracing and cost
tracking are applied in one place. Providers raise one of two errors, and the caller
decides from the type alone whether to try again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from invoice_agent.llm.tools import ToolCall, ToolSpec


class LlmError(Exception):
    """A model call failed. The message is safe to store: it never carries document text."""


class LlmTransientError(LlmError):
    """Rate limit, overload, timeout or network failure: trying again later may work."""


class LlmPermanentError(LlmError):
    """Bad request, authentication, refusal or truncated output: trying again cannot help."""


@dataclass(frozen=True)
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LlmResult[T: BaseModel]:
    parsed: T
    usage: LlmUsage
    cost_usd: Decimal
    model: str
    latency_s: float
    request_id: str | None
    stop_reason: str
    tool_calls: tuple[ToolCall, ...] = ()
    """The tools the model called, in order (empty for a single call)."""


class LlmProvider(Protocol):
    @property
    def model(self) -> str: ...

    def generate[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, name: str
    ) -> LlmResult[T]:
        """One call returning ``schema``, validated. ``name`` labels the call in traces."""
        ...


class ToolProvider(LlmProvider, Protocol):
    """A provider that can let the model call tools before it answers."""

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
        """A conversation in which the model may call ``tools``, then answers with ``schema``.

        Bounded on purpose: at most ``max_tool_calls`` calls and ``max_turns`` requests. When a
        bound is reached the model is told to answer with what it has. ``usage`` and
        ``cost_usd`` cover every request of the conversation.
        """
        ...
