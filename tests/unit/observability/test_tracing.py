import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from invoice_agent.llm.provider import LlmPermanentError
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing, mask
from tests.factories import GROUND_TRUTH
from tests.fakes import FakeProvider
from tests.unit.llm.test_extractor import reply_for

IBAN = "FR7699053965263585803695442"


@pytest.fixture
def traced() -> Iterator[tuple[Tracing, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid4().hex}",  # the SDK caches clients by public key
        secret_key="sk-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        mask=mask,
    )
    tracing = Tracing(client)
    yield tracing, exporter
    client.shutdown()


def spans(tracing: Tracing, exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    tracing.flush()
    return {span.name: span for span in exporter.get_finished_spans()}


# --- masking ---------------------------------------------------------------------------------


def test_masking_removes_ibans_and_api_keys_everywhere() -> None:
    data = {
        "text": f"pay {IBAN} now",
        "nested": [{"key": "sk-ant-api03-abcdefghij"}, ("FR76-9905-3965-2635-8580-3695-442",)],
        "number": 3,
        "none": None,
    }
    masked = json.dumps(mask(data=data))
    assert IBAN not in masked
    assert "9905" not in masked
    assert "sk-ant-api03" not in masked
    assert "[IBAN redacted]" in masked
    assert "[secret redacted]" in masked
    assert mask(data=data)["number"] == 3


def test_langfuse_secret_keys_are_masked_too() -> None:
    assert mask(data="key=sk-lf-0123456789abcdef") == "key=[secret redacted]"


def test_ordinary_text_is_left_alone() -> None:
    assert mask(data="Facture PM-2026-0457 du 17/09/2026, total 360,00 €") == (
        "Facture PM-2026-0457 du 17/09/2026, total 360,00 €"
    )


# --- disabled tracing --------------------------------------------------------------------------


def test_without_keys_tracing_is_disabled_and_harmless() -> None:
    tracing = Tracing.from_env({})
    assert tracing.enabled is False
    with tracing.observation("anything", as_type="generation", model="m") as observation:
        observation.update(output="x", usage_details={"input": 1})
    tracing.score("ok", 1)
    tracing.flush()
    tracing.shutdown()
    assert tracing.trace_id_for("seed") is None
    assert tracing.current_trace_id() is None


def test_one_missing_key_is_enough_to_disable_tracing() -> None:
    assert Tracing.from_env({"LANGFUSE_PUBLIC_KEY": "pk-x"}).enabled is False
    assert Tracing.from_env({"LANGFUSE_SECRET_KEY": "sk-x"}).enabled is False


# --- enabled tracing ---------------------------------------------------------------------------


def test_a_generation_records_model_usage_cost_and_masked_text(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    with tracing.observation(
        "extract_invoice", as_type="generation", model="claude-haiku-5-5", input=f"pay {IBAN}"
    ) as generation:
        generation.update(
            output={"total": "12.50"},
            usage_details={"input": 1800, "output": 300},
            cost_details={"total": 0.00033},
        )
    attributes = spans(tracing, exporter)["extract_invoice"].attributes
    assert attributes is not None
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.model.name"] == "claude-haiku-5-5"
    assert json.loads(str(attributes["langfuse.observation.usage_details"])) == {
        "input": 1800,
        "output": 300,
    }
    assert json.loads(str(attributes["langfuse.observation.cost_details"])) == {"total": 0.00033}
    assert IBAN not in json.dumps(dict(attributes))
    assert "[IBAN redacted]" in str(attributes["langfuse.observation.input"])


def test_the_same_seed_gives_the_same_trace_and_children_share_it(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    trace_id = tracing.trace_id_for("job-1")
    assert trace_id == tracing.trace_id_for("job-1") != tracing.trace_id_for("job-2")
    with tracing.observation("job.ingest", trace_id=trace_id) as job, tracing.observation("step"):
        assert job.trace_id == trace_id
        assert tracing.current_trace_id() == trace_id
    recorded = spans(tracing, exporter)
    assert {format(s.context.trace_id, "032x") for s in recorded.values()} == {trace_id}
    assert recorded["step"].parent is not None
    assert recorded["step"].parent.span_id == recorded["job.ingest"].context.span_id


def test_a_second_job_can_join_the_trace_of_the_first(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    with tracing.observation("job.ingest", trace_id=tracing.trace_id_for("job-1")) as first:
        pass
    with tracing.observation("job.validate", trace_id=first.trace_id) as second:
        pass
    assert second.trace_id == first.trace_id
    tracing.flush()
    assert len({s.context.trace_id for s in exporter.get_finished_spans()}) == 1


def test_scores_are_attached_to_the_current_trace(
    traced: tuple[Tracing, InMemorySpanExporter], monkeypatch: pytest.MonkeyPatch
) -> None:
    tracing, _ = traced
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tracing._client, "score_current_trace", lambda **kwargs: sent.append(kwargs)
    )
    with tracing.observation("job.ingest"):
        tracing.score("extraction_ok", 1, "grounded")
    assert sent == [
        {"name": "extraction_ok", "value": 1.0, "data_type": "NUMERIC", "comment": "grounded"}
    ]


# --- the traced provider -----------------------------------------------------------------------


def test_the_traced_provider_records_one_generation_per_call(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    inner = FakeProvider(reply_for(GROUND_TRUTH / "INV-09.json"))
    provider = TracedProvider(inner, tracing, prompt_label="extract_invoice.v1@abc12345")
    from invoice_agent.llm.extraction_schema import ExtractedInvoice

    result = provider.generate(
        system="SYSTEM PROMPT TEXT",
        user=f"<document>invoice {IBAN}</document>",
        schema=ExtractedInvoice,
        max_tokens=3000,
        name="extract_invoice",
    )
    assert result.cost_usd > Decimal(0)
    attributes = spans(tracing, exporter)["extract_invoice"].attributes
    assert attributes is not None
    assert attributes["langfuse.observation.model.name"] == "claude-haiku-5-5"
    blob = json.dumps(dict(attributes))
    assert "extract_invoice.v1@abc12345" in blob
    assert "req_fake" in blob
    assert "SYSTEM PROMPT TEXT" not in blob  # the prompt lives in git; only its label is sent
    assert IBAN not in blob
    usage = json.loads(str(attributes["langfuse.observation.usage_details"]))
    assert usage["output"] == 300
    assert json.loads(str(attributes["langfuse.observation.cost_details"]))["total"] > 0


def test_a_failed_call_is_recorded_as_an_error_and_still_raised(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    provider = TracedProvider(
        FakeProvider(LlmPermanentError("model request rejected (HTTP 400)")), tracing
    )
    from invoice_agent.llm.extraction_schema import ExtractedInvoice

    with pytest.raises(LlmPermanentError):
        provider.generate(
            system="s", user="u", schema=ExtractedInvoice, max_tokens=10, name="extract_invoice"
        )
    attributes = spans(tracing, exporter)["extract_invoice"].attributes
    assert attributes is not None
    assert attributes["langfuse.observation.level"] == "ERROR"
    assert "HTTP 400" in str(attributes["langfuse.observation.status_message"])


def test_the_traced_provider_works_with_tracing_disabled() -> None:
    provider = TracedProvider(FakeProvider(reply_for(GROUND_TRUTH / "INV-09.json")), Tracing())
    from invoice_agent.llm.extraction_schema import ExtractedInvoice

    result = provider.generate(
        system="s", user="u", schema=ExtractedInvoice, max_tokens=10, name="extract_invoice"
    )
    assert result.parsed.invoice_number


def test_identifiers_we_generated_survive_the_mask_under_their_keys_only() -> None:
    import random
    import uuid

    rng = random.Random(7)
    for _ in range(5_000):
        ident = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        assert mask(data={"run_id": ident, "metadata": {"trace_id": ident.replace("-", "")}}) == {
            "run_id": ident,
            "metadata": {"trace_id": ident.replace("-", "")},
        }
    from tests.factories import uuid_shaped_iban

    hidden = uuid_shaped_iban()
    assert "[IBAN redacted]" in json.dumps(mask(data={"note": hidden}))
