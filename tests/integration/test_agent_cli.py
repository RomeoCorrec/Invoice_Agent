"""The review command: list what the agent paused on, then decide."""

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.__main__ import main
from invoice_agent.agent.graph.run import start_run
from invoice_agent.db.repositories import InvoiceRepository
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.worker.store import LocalDocumentStore
from tests.factories import IBAN_KEY
from tests.integration.reference import load_reference
from tests.integration.support import agent_graph
from tests.integration.test_agent_graph import lookup_extractor
from tests.pdfs import INVOICES


@pytest.fixture
def waiting(
    engine: Engine, saver: Any, tmp_path: Path, postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[sessionmaker[Session], dict[str, UUID]]:
    """INV-01 (clean) and INV-12 (one anomaly), both paused for approval."""
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        load_reference(session)
    root = tmp_path / "documents"
    root.mkdir()
    graph = agent_graph(saver, factory, LocalDocumentStore(root), lookup_extractor())
    ids: dict[str, UUID] = {}
    for case in ("INV-01", "INV-12"):
        [pdf] = INVOICES.glob(f"{case}_*.pdf")
        shutil.copy(pdf, root / pdf.name)
        run = start_run(graph, run_id=uuid4(), document=pdf.name)
        ids[case] = UUID(run.state["invoice_id"])
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("IBAN_HASH_KEY", IBAN_KEY.decode().ljust(16, "k"))
    monkeypatch.setenv("DOCUMENT_ROOT", str(root))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    return factory, ids


def status(factory: sessionmaker[Session], invoice_id: UUID) -> InvoiceStatus:
    with session_scope(factory) as session:
        invoice = InvoiceRepository(session).get(invoice_id)
        assert invoice is not None
        return invoice.status


def test_pending_lists_the_invoices_with_their_anomalies(
    waiting: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _, ids = waiting
    assert main(["pending"]) == 0
    out = capsys.readouterr().out
    assert str(ids["INV-01"]) in out and str(ids["INV-12"]) in out
    assert "needs_review" in out and "VAT_AMOUNT_MISMATCH" in out


def test_approve_and_reject_decide_and_then_nothing_is_pending(
    waiting: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, ids = waiting
    assert main(["approve", str(ids["INV-01"]), "--by", "alice@example.com"]) == 0
    assert main(["reject", str(ids["INV-12"]), "--by", "alice@example.com", "--comment", "x"]) == 0
    assert status(factory, ids["INV-01"]) is InvoiceStatus.APPROVED
    assert status(factory, ids["INV-12"]) is InvoiceStatus.REJECTED
    capsys.readouterr()
    assert main(["pending"]) == 0
    assert "nothing is waiting" in capsys.readouterr().out


def test_deciding_twice_or_on_an_unknown_invoice_fails_cleanly(
    waiting: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, ids = waiting
    assert main(["approve", str(ids["INV-01"]), "--by", "alice"]) == 0
    assert main(["reject", str(ids["INV-01"]), "--by", "bob"]) == 1
    assert main(["approve", str(uuid4()), "--by", "alice"]) == 1
    assert "no run is waiting" in capsys.readouterr().err
    assert status(factory, ids["INV-01"]) is InvoiceStatus.APPROVED


def test_an_empty_approver_is_refused_before_anything_happens(
    waiting: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, ids = waiting
    assert main(["approve", str(ids["INV-01"]), "--by", "  "]) == 2
    assert "invalid decision: approver" in capsys.readouterr().err
    assert status(factory, ids["INV-01"]) is InvoiceStatus.VALIDATED


def test_seed_loads_the_demo_suppliers_and_orders(
    engine: Engine,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("IBAN_HASH_KEY", IBAN_KEY.decode().ljust(16, "k"))
    assert main(["seed"]) == 0
    assert "loaded" in capsys.readouterr().out
    with engine.connect() as connection:
        suppliers = connection.execute(text("SELECT count(*) FROM suppliers")).scalar_one()
        orders = connection.execute(text("SELECT count(*) FROM purchase_orders")).scalar_one()
    assert suppliers >= 5 and orders >= 5


def test_text_from_a_document_cannot_drive_the_terminal(
    waiting: Any, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hostile invoice number or supplier name must not smuggle escape sequences."""
    hostile = "A\x1b[2J\x1b[31m\r‮FAKE approved"
    with engine.begin() as connection:
        connection.execute(text("UPDATE invoices SET invoice_number = :n"), {"n": hostile})
    assert main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "‮" not in out
    assert len(out.splitlines()) == 2  # still one line per invoice
