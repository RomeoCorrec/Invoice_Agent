"""Building an agent graph for tests."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.build import build_graph
from invoice_agent.agent.graph.deps import AgentDeps, utc_now
from invoice_agent.agent.graph.run import Graph
from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.reading import RawDocument, read_document
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.store import DocumentStore
from tests.factories import IBAN_KEY


def agent_graph(
    saver: BaseCheckpointSaver[Any],
    factory: sessionmaker[Session],
    store: DocumentStore,
    extractor: InvoiceExtractor | None,
    *,
    reader: Callable[..., RawDocument] = read_document,
    tracing: Tracing | None = None,
    clock: Callable[[], datetime] = utc_now,
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
        ),
        saver,
    )
