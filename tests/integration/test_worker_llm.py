"""The worker with the LLM extractor and Langfuse tracing, against a scripted provider."""

import json
import shutil
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.config import IngestionLimits, WorkerSettings
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository, JobRepository
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import JobStatus
from invoice_agent.llm.extractor import LlmInvoiceExtractor
from invoice_agent.llm.provider import LlmTransientError
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing, mask
from invoice_agent.worker.handlers import HANDLERS, JobContext, ingest_job
from invoice_agent.worker.runner import Worker
from invoice_agent.worker.store import LocalDocumentStore
from tests.factories import GROUND_TRUTH, IBAN_KEY, T0
from tests.fakes import FakeProvider
from tests.integration.reference import load_reference
from tests.pdfs import INVOICES
from tests.unit.llm.test_extractor import reply_for

SETTINGS = WorkerSettings(poll_interval_s=0.05, job_timeout_s=60, max_attempts=3)
IBAN_IN_INVOICE = "FR7699065841650681552315323"  # printed on INV-09


@pytest.fixture
def traced() -> Iterator[tuple[Tracing, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid4().hex}",
        secret_key="sk-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        mask=mask,
    )
    client.score_current_trace = lambda **_kwargs: None  # type: ignore[method-assign]
    yield Tracing(client), exporter
    client.shutdown()


def build(
    factory: sessionmaker[Session], root: Path, provider: FakeProvider, tracing: Tracing
) -> Worker:
    extractor = LlmInvoiceExtractor(
        TracedProvider(provider, tracing, prompt_label="extract_invoice.v1@test"), IBAN_KEY
    )
    store = LocalDocumentStore(root)

    def context_for(session: Session, now: Any) -> JobContext:
        return JobContext(
            session=session,
            now=now,
            store=store,
            extractor=extractor,
            iban_key=IBAN_KEY,
            limits=IngestionLimits(),
            max_attempts=SETTINGS.max_attempts,
            tracing=tracing,
        )

    return Worker(
        factory,
        worker_id="w1",
        handlers=HANDLERS,
        context_for=context_for,
        settings=SETTINGS,
        clock=lambda: T0 + timedelta(hours=1),
        rng=lambda: 0.0,
        tracing=tracing,
    )


def prepare(engine: Engine, tmp_path: Path) -> sessionmaker[Session]:
    [pdf] = INVOICES.glob("INV-09_*.pdf")
    shutil.copy(pdf, tmp_path / pdf.name)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        load_reference(session)
        JobRepository(session).enqueue(ingest_job(pdf.name, now=T0, max_attempts=3))
    return factory


def test_one_invoice_is_one_trace_from_the_extraction_to_the_validation(
    engine: Engine, tmp_path: Path, traced: tuple[Tracing, InMemorySpanExporter]
) -> None:
    tracing, exporter = traced
    factory = prepare(engine, tmp_path)
    provider = FakeProvider(reply_for(GROUND_TRUTH / "INV-09.json"))
    assert build(factory, tmp_path, provider, tracing).run_until_idle() == 2  # ingest + validate

    tracing.flush()
    recorded = {span.name: span for span in exporter.get_finished_spans()}
    assert {"job.ingest", "extract_invoice", "job.validate"} <= set(recorded)
    trace_ids = {format(span.context.trace_id, "032x") for span in recorded.values()}
    assert len(trace_ids) == 1  # the validation job joined the trace of the ingestion job

    generation = recorded["extract_invoice"].attributes
    assert generation is not None
    assert generation["langfuse.observation.model.name"] == "claude-haiku-5-5"
    assert "extract_invoice.v1@test" in json.dumps(dict(generation))

    # the audit trail carries the trace id, so a decision can be followed in both tools
    with session_scope(factory) as session:
        [invoice] = InvoiceRepository(session).list_by_status(InvoiceStatus.VALIDATED)
        events = AuditEventRepository(session).list_for_invoice(invoice.id)
    assert [event.step for event in events] == ["receive", "extract", "validate"]
    assert {event.payload["trace_id"] for event in events} == trace_ids


def test_nothing_sent_to_langfuse_contains_the_invoice_iban(
    engine: Engine, tmp_path: Path, traced: tuple[Tracing, InMemorySpanExporter]
) -> None:
    tracing, exporter = traced
    factory = prepare(engine, tmp_path)
    provider = FakeProvider(reply_for(GROUND_TRUTH / "INV-09.json"))
    build(factory, tmp_path, provider, tracing).run_until_idle()
    tracing.flush()
    everything = json.dumps(
        [dict(span.attributes or {}) for span in exporter.get_finished_spans()], default=str
    )
    assert IBAN_IN_INVOICE not in everything
    assert "9906 5841" not in everything
    # the model did receive the document text, with the IBAN in it: only the traces are masked
    assert "9906" in provider.calls[0]["user"]


def test_a_provider_outage_retries_the_job_instead_of_failing_the_invoice(
    engine: Engine, tmp_path: Path, traced: tuple[Tracing, InMemorySpanExporter]
) -> None:
    tracing, _ = traced
    factory = prepare(engine, tmp_path)
    calls: list[int] = []
    reply = reply_for(GROUND_TRUTH / "INV-09.json")

    def flaky(_user: str) -> Any:
        calls.append(1)
        return (
            LlmTransientError("model provider unavailable (HTTP 529)") if len(calls) == 1 else reply
        )

    worker = build(factory, tmp_path, FakeProvider(flaky), tracing)
    assert worker.run_once() is True  # attempt 1: the provider is overloaded
    with session_scope(factory) as session:
        counts = JobRepository(session).count_by_status()
        assert InvoiceRepository(session).list_by_status(InvoiceStatus.FAILED) == []
        error = session.execute(
            text("SELECT last_error FROM jobs WHERE kind = 'ingest'")
        ).scalar_one()
    assert counts == {JobStatus.QUEUED: 1}  # retried later, not lost
    assert "529" in error

    # the backoff elapsed (the clock of the test is fixed, so move the job's date back)
    with session_scope(factory) as session:
        session.execute(text("UPDATE jobs SET run_after = :d"), {"d": T0})
    assert worker.run_until_idle() == 2  # the retry succeeds, then the validation runs
    with session_scope(factory) as session:
        assert len(InvoiceRepository(session).list_by_status(InvoiceStatus.VALIDATED)) == 1
