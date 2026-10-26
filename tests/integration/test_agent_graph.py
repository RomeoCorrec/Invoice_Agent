"""The agent graph against a real Postgres: the pause for a human, resuming, and surviving
crashes (the database holds both the business data and the checkpoints)."""

import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph import build
from invoice_agent.agent.graph.build import build_graph
from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.nodes import StaleApprovalError
from invoice_agent.agent.graph.run import (
    Graph,
    NoPendingApprovalError,
    RunStatus,
    resume_run,
    run_id_for_invoice,
    start_run,
)
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict
from invoice_agent.config import IngestionLimits
from invoice_agent.db.models import InvoiceRow
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import make_session_factory, session_scope
from invoice_agent.domain.audit import ActorType
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import TransientJobError
from invoice_agent.worker.store import LocalDocumentStore
from tests.factories import GROUND_TRUTH, IBAN_KEY, golden_extraction
from tests.fakes import LookupExtractor
from tests.integration.reference import load_reference
from tests.pdfs import INVOICES

APPROVE = ApprovalDecision(verdict=Verdict.APPROVE, approver="alice@example.com")
REJECT = ApprovalDecision(verdict=Verdict.REJECT, approver="alice@example.com", comment="no")


class SteppingClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def lookup_extractor() -> LookupExtractor:
    by_number = {}
    for path in sorted(GROUND_TRUTH.glob("INV-*.json")):
        data = golden_extraction(path)
        by_number[data.invoice_number] = data
    return LookupExtractor(by_number)


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        load_reference(session)
    yield factory


@pytest.fixture
def documents(tmp_path: Path) -> Path:
    root = tmp_path / "documents"
    root.mkdir()
    for case in ("INV-01", "INV-09", "INV-12"):
        [source] = INVOICES.glob(f"{case}_*.pdf")
        shutil.copy(source, root / source.name)
    return root


def document(documents: Path, case: str) -> str:
    [path] = documents.glob(f"{case}_*.pdf")
    return path.name


@pytest.fixture
def connection(postgres_url: str) -> Iterator[Connection[Any]]:
    dsn = postgres_url.replace("+psycopg", "")
    with Connection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as c:
        yield c


NOT_GIVEN: Any = object()


def graph_for(
    factory: sessionmaker[Session],
    documents: Path,
    connection: Connection[Any],
    *,
    extractor: Any = NOT_GIVEN,
) -> Graph:
    saver = PostgresSaver(connection)
    saver.setup()
    deps = AgentDeps(
        session_factory=factory,
        store=LocalDocumentStore(documents),
        extractor=lookup_extractor() if extractor is NOT_GIVEN else extractor,
        iban_key=IBAN_KEY,
        limits=IngestionLimits(),
        clock=SteppingClock(),
    )
    return build_graph(deps, saver)


@pytest.fixture
def graph_factory(
    factory: sessionmaker[Session], documents: Path, connection: Connection[Any]
) -> Callable[[], Graph]:
    """Builds the graph on demand, so a test can patch a node before the graph exists."""
    return lambda: graph_for(factory, documents, connection)


@pytest.fixture
def graph(graph_factory: Callable[[], Graph]) -> Graph:
    return graph_factory()


def start(graph: Graph, name: str, run_id: UUID | None = None) -> RunStatus:
    return start_run(graph, run_id=run_id or uuid4(), document=name)


def invoice_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.execute(select(func.count()).select_from(InvoiceRow)).scalar_one()


def status_of(factory: sessionmaker[Session], invoice_id: str) -> InvoiceStatus:
    with session_scope(factory) as session:
        invoice = InvoiceRepository(session).get(UUID(invoice_id))
        assert invoice is not None
        return invoice.status


def steps(factory: sessionmaker[Session], run_id: UUID) -> list[str]:
    with session_scope(factory) as session:
        return [e.step for e in AuditEventRepository(session).list_for_run(run_id)]


# --- the pause -----------------------------------------------------------------------------


def test_a_clean_invoice_stops_for_a_human_with_a_summary(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-01"))
    assert not run.finished
    request = run.awaiting_approval
    assert request is not None
    assert request["invoice_number"] == "PM-2026-0457" and request["status"] == "validated"
    assert request["anomaly_codes"] == [] and request["total_ttc"] == str(
        golden_extraction(GROUND_TRUTH / "INV-01.json").total_ttc
    )
    assert set(request) == {
        "invoice_id", "invoice_number", "seller", "currency",
        "total_ht", "total_vat", "total_ttc", "status", "anomaly_codes",
    }  # fmt: skip
    # nothing is decided without a human
    assert status_of(factory, run.state["invoice_id"]) is InvoiceStatus.VALIDATED
    assert steps(factory, run.run_id) == ["receive", "extract", "validate"]


def test_an_invoice_with_an_anomaly_waits_as_needs_review(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-12"))
    assert run.awaiting_approval is not None
    assert run.awaiting_approval["anomaly_codes"] == ["VAT_AMOUNT_MISMATCH"]
    assert run.awaiting_approval["status"] == "needs_review"
    assert status_of(factory, run.state["invoice_id"]) is InvoiceStatus.NEEDS_REVIEW


def test_a_document_that_cannot_be_read_ends_the_run_as_failed(
    factory: sessionmaker[Session], documents: Path, connection: Connection[Any]
) -> None:
    # INV-09 is a plain PDF: with no extractor configured it cannot be read
    graph = graph_for(factory, documents, connection, extractor=None)
    run = start(graph, document(documents, "INV-09"))
    assert run.finished and run.awaiting_approval is None
    assert status_of(factory, run.state["invoice_id"]) is InvoiceStatus.FAILED
    assert steps(factory, run.run_id) == ["receive", "extract"]


# --- the decision --------------------------------------------------------------------------


def test_approving_marks_the_invoice_and_records_who_decided(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-01"))
    invoice_id = UUID(run.state["invoice_id"])
    done = resume_run(graph, factory, invoice_id, APPROVE)
    assert done.finished and done.awaiting_approval is None
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED
    with session_scope(factory) as session:
        events = AuditEventRepository(session).list_for_invoice(invoice_id)
    approval = events[-1]
    assert [e.step for e in events] == ["receive", "extract", "validate", "approval"]
    assert approval.actor_type is ActorType.HUMAN and approval.actor_id == "alice@example.com"
    assert approval.run_id == run.run_id
    assert approval.payload["verdict"] == "approve" and approval.payload["to"] == "approved"


def test_rejecting_is_terminal_and_keeps_the_comment(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-12"))
    invoice_id = UUID(run.state["invoice_id"])
    resume_run(graph, factory, invoice_id, REJECT)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.REJECTED
    with session_scope(factory) as session:
        approval = AuditEventRepository(session).list_for_invoice(invoice_id)[-1]
    assert approval.payload["comment"] == "no" and approval.payload["from"] == "needs_review"


def test_a_bank_account_typed_in_a_comment_never_reaches_the_audit_log(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-01"))
    invoice_id = UUID(run.state["invoice_id"])
    comment = "paid to FR7699053965263585803695442 as agreed"
    resume_run(
        graph,
        factory,
        invoice_id,
        ApprovalDecision(verdict=Verdict.APPROVE, approver="a", comment=comment),
    )
    with session_scope(factory) as session:
        stored = AuditEventRepository(session).list_for_invoice(invoice_id)[-1].payload["comment"]
    assert isinstance(stored, str) and "FR7699053965263585803695442" not in stored


def test_a_second_decision_changes_nothing(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-01"))
    invoice_id = UUID(run.state["invoice_id"])
    resume_run(graph, factory, invoice_id, APPROVE)
    with pytest.raises(NoPendingApprovalError):
        resume_run(graph, factory, invoice_id, REJECT)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED
    assert steps(factory, run.run_id).count("approval") == 1


def test_deciding_on_an_unknown_invoice_is_refused(
    graph: Graph, factory: sessionmaker[Session]
) -> None:
    with pytest.raises(NoPendingApprovalError):
        resume_run(graph, factory, uuid4(), APPROVE)


def test_a_decision_must_name_an_approver_and_nothing_else() -> None:
    for bad in (
        {"verdict": "approve", "approver": ""},
        {"verdict": "approve", "approver": "   "},
        {"verdict": "maybe", "approver": "a"},
        {"verdict": "approve", "approver": "a", "extra": 1},
        {"verdict": "approve", "approver": "a" * 101},
    ):
        with pytest.raises(ValueError):
            ApprovalDecision.model_validate(bad)


def test_an_invoice_that_changed_meanwhile_is_not_decided(
    graph: Graph, factory: sessionmaker[Session], documents: Path
) -> None:
    run = start(graph, document(documents, "INV-01"))
    invoice_id = UUID(run.state["invoice_id"])
    with session_scope(factory) as session:
        repo = InvoiceRepository(session)
        invoice = repo.get(invoice_id)
        assert invoice is not None
        repo.save(
            invoice.transition_to(
                InvoiceStatus.FAILED, at=datetime.now(UTC), failure_reason="withdrawn"
            ),
            expected_status=InvoiceStatus.VALIDATED,
        )
    with pytest.raises(StaleApprovalError):
        resume_run(graph, factory, invoice_id, APPROVE)
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.FAILED
    assert "approval" not in steps(factory, run.run_id)


# --- crashes and restarts ------------------------------------------------------------------


def test_a_run_waiting_for_a_human_survives_a_restart(
    factory: sessionmaker[Session], documents: Path, postgres_url: str
) -> None:
    dsn = postgres_url.replace("+psycopg", "")
    with Connection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as c1:
        first = graph_for(factory, documents, c1)
        run = start(first, document(documents, "INV-01"))
    invoice_id = UUID(run.state["invoice_id"])
    # a new process: new connection, new graph, nothing in memory
    with Connection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as c2:
        second = graph_for(factory, documents, c2)
        done = resume_run(second, factory, invoice_id, APPROVE)
    assert done.finished
    assert status_of(factory, str(invoice_id)) is InvoiceStatus.APPROVED


def crash_after_commit(real: Callable[[AgentDeps], Any]) -> Callable[[AgentDeps], Any]:
    """A node that does its work, commits, then dies once before the checkpoint is written."""

    def factory(deps: AgentDeps) -> Any:
        node = real(deps)
        armed = [True]

        def wrapped(state: Any) -> dict[str, Any]:
            result: dict[str, Any] = node(state)
            if armed[0]:
                armed[0] = False
                raise RuntimeError("the process died after the commit")
            return result

        return wrapped

    return factory


def test_a_node_replayed_after_a_crash_does_not_ingest_twice(
    graph_factory: Callable[[], Graph],
    engine: Engine,
    factory: sessionmaker[Session],
    documents: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build, "make_ingest", crash_after_commit(build.make_ingest))
    graph = graph_factory()
    run_id = uuid4()
    with pytest.raises(RuntimeError, match="died"):
        start(graph, document(documents, "INV-01"), run_id)
    assert invoice_count(engine) == 1  # the node committed before dying
    run = start(graph, document(documents, "INV-01"), run_id)
    assert run.awaiting_approval is not None
    assert invoice_count(engine) == 1
    assert steps(factory, run_id) == ["receive", "extract", "validate"]


def test_validation_replayed_after_a_crash_reports_what_it_recorded(
    graph_factory: Callable[[], Graph],
    factory: sessionmaker[Session],
    documents: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build, "make_validate", crash_after_commit(build.make_validate))
    graph = graph_factory()
    run_id = uuid4()
    with pytest.raises(RuntimeError, match="died"):
        start(graph, document(documents, "INV-12"), run_id)
    run = start(graph, document(documents, "INV-12"), run_id)
    assert run.awaiting_approval is not None
    assert run.awaiting_approval["anomaly_codes"] == ["VAT_AMOUNT_MISMATCH"]
    assert steps(factory, run_id).count("validate") == 1


def test_a_transient_failure_stops_the_run_and_a_retry_completes_it(
    factory: sessionmaker[Session],
    documents: Path,
    connection: Connection[Any],
    engine: Engine,
) -> None:
    class Flaky:
        calls = 0

        def extract(self, text: str) -> Any:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise TransientJobError("model provider unavailable")
            return lookup_extractor().extract(text)

    graph = graph_for(factory, documents, connection, extractor=Flaky())
    run_id = uuid4()
    with pytest.raises(TransientJobError):
        start(graph, document(documents, "INV-09"), run_id)
    assert invoice_count(engine) == 0  # nothing half-written
    run = start(graph, document(documents, "INV-09"), run_id)
    assert run.awaiting_approval is not None and invoice_count(engine) == 1


def test_starting_a_run_twice_is_harmless(
    graph: Graph, engine: Engine, factory: sessionmaker[Session], documents: Path
) -> None:
    run_id = uuid4()
    first = start(graph, document(documents, "INV-01"), run_id)
    again = start(graph, document(documents, "INV-01"), run_id)
    assert again.awaiting_approval == first.awaiting_approval
    assert invoice_count(engine) == 1 and steps(factory, run_id) == [
        "receive",
        "extract",
        "validate",
    ]
    with session_scope(factory) as session:
        assert run_id_for_invoice(session, UUID(first.state["invoice_id"])) == run_id


def test_the_graph_draws_itself(graph: Graph) -> None:
    mermaid = graph.get_graph().draw_mermaid()
    for node in ("ingest", "validate", "request_approval", "apply_decision"):
        assert node in mermaid
