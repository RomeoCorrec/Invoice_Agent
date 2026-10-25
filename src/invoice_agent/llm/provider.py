"""The seam between the application and a language model provider.

Every LLM call goes through a ``LlmProvider`` so that timeouts, retries, tracing and cost
tracking are applied in one place. Providers raise one of two errors, and the caller
decides from the type alone whether to try again.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel


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


class LlmProvider(Protocol):
    @property
    def model(self) -> str: ...

    def generate[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, name: str
    ) -> LlmResult[T]:
        """One call returning ``schema``, validated. ``name`` labels the call in traces."""
        ...
