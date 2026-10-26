"""What the graph's nodes need from the outside world, gathered in one place."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.reading import RawDocument, read_document
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.store import DocumentStore


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AgentDeps:
    session_factory: sessionmaker[Session]
    """Each node opens its own transaction: that is what lets a run resume after a crash."""
    store: DocumentStore
    extractor: InvoiceExtractor | None
    iban_key: bytes
    limits: IngestionLimits
    reader: Callable[..., RawDocument] = read_document
    tracing: Tracing = field(default_factory=Tracing)
    clock: Callable[[], datetime] = utc_now
