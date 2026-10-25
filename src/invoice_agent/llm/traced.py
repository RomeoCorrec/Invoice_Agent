"""A provider wrapper that records every call as a Langfuse generation."""

from pydantic import BaseModel

from invoice_agent.llm.provider import LlmError, LlmProvider, LlmResult
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
        with self._tracing.observation(
            name,
            as_type="generation",
            model=self._inner.model,
            input=user,
            metadata={"prompt": self._prompt_label, "max_tokens": max_tokens},
        ) as observation:
            try:
                result = self._inner.generate(
                    system=system, user=user, schema=schema, max_tokens=max_tokens, name=name
                )
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
                },
            )
            return result
