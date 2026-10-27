"""A document the worker gives up on must not leave a half-processed invoice behind."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.failure import fail_invoice_of_run
from invoice_agent.agent.graph import build
from invoice_agent.agent.ingestion import ingest_document
from invoice_agent.agent.validation import validate_invoice
from invoice_agent.config import IngestionLimits
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository, JobRepository
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.invoice import Invoice
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import Job, JobStatus
from invoice_agent.worker.handlers import process_job
from tests.factories import IBAN_KEY, T0
from tests.integration.reference import load_reference
from tests.integration.test_agent_graph import lookup_extractor
from tests.integration.test_worker_end_to_end import worker_for
from tests.pdfs import INVOICES

PASSWORD = "password=hunter2"


def prepare(engine: Engine, tmp_path: Path, max_attempts: int) -> tuple[sessionmaker[Session], Job]:
    [pdf] = INVOICES.glob("INV-01_*.pdf")
    shutil.copy(pdf, tmp_path / pdf.name)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        load_reference(session)
        job = JobRepository(session).enqueue(
            process_job(pdf.name, now=T0, max_attempts=max_attempts)
        )
    return factory, job


def break_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingestion succeeds, then validation always fails."""

    def factory(_deps: Any) -> Any:
        def validate(state: Any) -> dict[str, Any]:
            raise RuntimeError(f"validation crashed ({PASSWORD})")

        return validate

    monkeypatch.setattr(build, "make_validate", factory)


def only_invoice(factory: sessionmaker[Session]) -> Invoice:
    with session_scope(factory) as session:
        [row] = session.execute(text("SELECT id FROM invoices")).all()
        invoice = InvoiceRepository(session).get(row[0])
    assert invoice is not None
    return invoice


def test_a_job_that_gives_up_fails_its_invoice_with_the_reason(
    engine: Engine, saver: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, job = prepare(engine, tmp_path, max_attempts=1)
    break_validation(monkeypatch)
    worker_for(factory, tmp_path, saver).run_until_idle()

    with session_scope(factory) as session:
        dead = JobRepository(session).get(job.id)
    assert dead is not None and dead.status is JobStatus.DEAD
    invoice = only_invoice(factory)
    assert invoice.status is InvoiceStatus.FAILED
    assert "RuntimeError" in (invoice.failure_reason or "")
    with session_scope(factory) as session:
        events = AuditEventRepository(session).list_for_invoice(invoice.id)
    assert [e.step for e in events] == ["receive", "extract", "fail"]
    assert events[-1].run_id == job.id and events[-1].payload["from"] == "extracted"


def test_a_job_that_will_be_retried_leaves_its_invoice_alone(
    engine: Engine, saver: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, job = prepare(engine, tmp_path, max_attempts=3)
    break_validation(monkeypatch)
    assert worker_for(factory, tmp_path, saver).run_once() is True
    with session_scope(factory) as session:
        queued = JobRepository(session).get(job.id)
    assert queued is not None and queued.status is JobStatus.QUEUED
    assert only_invoice(factory).status is InvoiceStatus.EXTRACTED


def test_a_job_that_dies_before_storing_an_invoice_has_nothing_to_fail(
    engine: Engine, saver: Any, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        job = JobRepository(session).enqueue(process_job("nowhere.pdf", now=T0, max_attempts=3))
    worker_for(factory, tmp_path, saver).run_until_idle()
    with session_scope(factory) as session:
        dead = JobRepository(session).get(job.id)
        assert dead is not None and dead.status is JobStatus.DEAD
        assert session.execute(text("SELECT count(*) FROM invoices")).scalar_one() == 0


def test_a_failing_hook_does_not_keep_the_job_alive(
    engine: Engine, saver: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, job = prepare(engine, tmp_path, max_attempts=1)
    break_validation(monkeypatch)

    def broken_hook(*_args: Any) -> None:
        raise RuntimeError("the hook is broken")

    worker_for(factory, tmp_path, saver, on_dead=broken_hook).run_until_idle()
    with session_scope(factory) as session:
        dead = JobRepository(session).get(job.id)
    assert dead is not None and dead.status is JobStatus.DEAD
    assert only_invoice(factory).status is InvoiceStatus.EXTRACTED  # untouched, job is dead


def test_an_invoice_that_already_reached_a_final_status_is_left_alone(
    engine: Engine, tmp_path: Path
) -> None:
    factory, _ = prepare(engine, tmp_path, max_attempts=1)
    run_id = uuid4()
    [pdf] = INVOICES.glob("INV-01_*.pdf")
    now = datetime.now(UTC)
    with session_scope(factory) as session:
        invoice = ingest_document(
            session,
            pdf.read_bytes(),
            pdf.name,
            extractor=lookup_extractor(),
            iban_key=IBAN_KEY,
            limits=IngestionLimits(),
            run_id=run_id,
            now=now,
        )
        validate_invoice(session, invoice.id, run_id=run_id, now=now)
        repo = InvoiceRepository(session)
        validated = repo.get(invoice.id)
        assert validated is not None
        repo.save(
            validated.transition_to(InvoiceStatus.REJECTED, at=now),
            expected_status=InvoiceStatus.VALIDATED,
        )
    with session_scope(factory) as session:
        assert fail_invoice_of_run(session, run_id, now=now, reason="x") is None
        final = InvoiceRepository(session).get(invoice.id)
    assert final is not None and final.status is InvoiceStatus.REJECTED
