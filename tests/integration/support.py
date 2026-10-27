"""Helpers shared by the integration tests of the agent (fixtures are in conftest.py)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.build import build_graph
from invoice_agent.agent.graph.deps import AgentDeps, ExplainConfig, utc_now
from invoice_agent.agent.graph.run import Graph, RunStatus, start_run
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict
from invoice_agent.config import IngestionLimits
from invoice_agent.db.models import InvoiceRow
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import session_scope
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.reading import RawDocument, read_document
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.store import DocumentStore, LocalDocumentStore
from tests.factories import GROUND_TRUTH, IBAN_KEY, golden_extraction
from tests.fakes import LookupExtractor

APPROVE = ApprovalDecision(verdict=Verdict.APPROVE, approver="alice@example.com")
REJECT = ApprovalDecision(verdict=Verdict.REJECT, approver="alice@example.com", comment="no")
NOT_GIVEN: Any = object()


class SteppingClock:
    """Each reading is one second after the previous one, so event order is deterministic."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def lookup_extractor() -> LookupExtractor:
    """Plays the model for every golden document."""
    by_number = {}
    for path in sorted(GROUND_TRUTH.glob("INV-*.json")):
        data = golden_extraction(path)
        by_number[data.invoice_number] = data
    return LookupExtractor(by_number)


def agent_graph(
    saver: BaseCheckpointSaver[Any],
    factory: sessionmaker[Session],
    store: DocumentStore,
    extractor: InvoiceExtractor | None,
    *,
    reader: Callable[..., RawDocument] = read_document,
    tracing: Tracing | None = None,
    clock: Callable[[], datetime] = utc_now,
    ledger: MCPServer | StdioServerParameters | None = None,
    explain: ExplainConfig | None = None,
) -> Graph:
    return build_graph(
        AgentDeps(
            session_factory=factory,
            store=store,
            extractor=extractor,
            iban_key=IBAN_KEY,
            limits=IngestionLimits(),
            reader=reader,
            tracing=tracing or Tracing(),
            clock=clock,
            ledger=ledger,
            explain=explain,
        ),
        saver,
    )


def graph_for(
    factory: sessionmaker[Session],
    documents: Path,
    saver: BaseCheckpointSaver[Any],
    *,
    extractor: Any = NOT_GIVEN,
    tracing: Tracing | None = None,
    ledger: MCPServer | StdioServerParameters | None = None,
    explain: ExplainConfig | None = None,
) -> Graph:
    """A graph over the golden documents, with a stepping clock."""
    return agent_graph(
        saver,
        factory,
        LocalDocumentStore(documents),
        lookup_extractor() if extractor is NOT_GIVEN else extractor,
        tracing=tracing,
        clock=SteppingClock(),
        ledger=ledger,
        explain=explain,
    )


def document(documents: Path, case: str) -> str:
    [path] = documents.glob(f"{case}_*.pdf")
    return path.name


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
