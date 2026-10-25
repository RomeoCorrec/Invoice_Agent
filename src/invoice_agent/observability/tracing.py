"""Langfuse tracing, behind a small interface that does nothing when Langfuse is not configured.

What leaves the process is masked first: IBANs and API keys are removed from every input,
output and metadata, whatever the caller passed. A trace follows one invoice through its
jobs: the ingestion job opens it (its id derives from the job id, so retries land in the same
trace) and hands the trace id to the validation job it queues.
"""

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from langfuse import Langfuse

from invoice_agent.domain.iban import redact_ibans

SECRET_PATTERN = re.compile(r"\bsk-(?:ant|lf)-[A-Za-z0-9_\-]{6,}")


def mask(*, data: Any, **_kwargs: Any) -> Any:
    """Remove bank details and API keys from anything about to be sent to Langfuse."""
    if isinstance(data, str):
        return SECRET_PATTERN.sub("[secret redacted]", redact_ibans(data))
    if isinstance(data, Mapping):
        return {key: mask(data=value) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [mask(data=item) for item in data]
    return data


class Observation:
    """A span or generation. Every method is a no-op when tracing is disabled."""

    def __init__(self, inner: Any | None, trace_id: str | None) -> None:
        self._inner = inner
        self.trace_id = trace_id

    def update(self, **fields: Any) -> None:
        if self._inner is not None:
            self._inner.update(**fields)


class Tracing:
    def __init__(self, client: Langfuse | None = None) -> None:
        self._client = client

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Tracing":
        """Enabled when both Langfuse keys are present; otherwise a silent no-op."""
        public, secret = environ.get("LANGFUSE_PUBLIC_KEY"), environ.get("LANGFUSE_SECRET_KEY")
        if not public or not secret:
            return cls(None)
        client = Langfuse(
            public_key=public,
            secret_key=secret,
            base_url=environ.get("LANGFUSE_BASE_URL", "http://localhost:3000"),
            environment=environ.get("LANGFUSE_ENVIRONMENT", "development"),
            mask=mask,
        )
        return cls(client)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def trace_id_for(self, seed: str) -> str | None:
        """A stable trace id derived from ``seed`` (None when disabled)."""
        return self._client.create_trace_id(seed=seed) if self._client else None

    def current_trace_id(self) -> str | None:
        return self._client.get_current_trace_id() if self._client else None

    @contextmanager
    def observation(
        self,
        name: str,
        *,
        as_type: str = "span",
        trace_id: str | None = None,
        input: Any = None,
        metadata: Any = None,
        model: str | None = None,
    ) -> Iterator[Observation]:
        """Open a span (or generation) as a child of the current one, or at the head of
        ``trace_id`` when given."""
        if self._client is None:
            yield Observation(None, None)
            return
        arguments: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "input": input,
            "metadata": metadata,
        }
        if model is not None:
            arguments["model"] = model
        if trace_id is not None:
            arguments["trace_context"] = {"trace_id": trace_id}
        with self._client.start_as_current_observation(**arguments) as inner:
            yield Observation(inner, self._client.get_current_trace_id())

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        """Attach a numeric score to the current trace."""
        if self._client is not None:
            self._client.score_current_trace(
                name=name, value=float(value), data_type="NUMERIC", comment=comment
            )

    def flush(self) -> None:
        if self._client is not None:
            self._client.flush()

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.shutdown()
