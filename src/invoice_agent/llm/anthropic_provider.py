"""Claude through the official Anthropic SDK.

One call per extraction, no tools, no sampling parameters (removed in the 2.x API), the
reply constrained to a JSON schema and validated by Pydantic (after the stop reason is
checked). The SDK already retries connection errors, 408/409/429 and 5xx with backoff; what
it gives up on is reported as a transient error so the worker can try again later with its
own backoff.

Error messages built here contain a status code or an error type, never the text of the
exception: the document being processed must not leak into logs or the job table.
"""

import time

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

TRANSIENT_STATUS = frozenset({408, 409, 429, 529})


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
        try:
            # create(), not parse(): parse() validates the text before we can look at the stop
            # reason, so a refusal (empty content) would be reported as a schema mismatch.
            message = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self._effort,
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(schema.model_json_schema()),
                    },
                },
            )
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if status >= 500 or status in TRANSIENT_STATUS:
                raise LlmTransientError(f"model provider unavailable (HTTP {status})") from exc
            raise LlmPermanentError(f"model request rejected (HTTP {status})") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LlmTransientError(f"model provider unreachable ({type(exc).__name__})") from exc

        stop_reason = message.stop_reason or "unknown"
        if stop_reason == "refusal":
            raise LlmPermanentError("the model declined to process this document")
        if stop_reason == "max_tokens":
            raise LlmPermanentError("the model reply was cut off (max_tokens)")
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LlmPermanentError("the model reply does not match the expected schema") from exc

        usage = LlmUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )
        return LlmResult(
            parsed=parsed,
            usage=usage,
            cost_usd=cost_usd(self._model, usage),
            model=self._model,
            latency_s=time.monotonic() - started,
            request_id=getattr(message, "_request_id", None),
            stop_reason=stop_reason,
        )
