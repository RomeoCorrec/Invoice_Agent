"""What the graph's nodes need from the outside world, gathered in one place."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.reading import RawDocument, read_document
from invoice_agent.llm.prompt import Prompt
from invoice_agent.llm.provider import ToolProvider
from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.ledger import CallPolicy
from invoice_agent.worker.store import DocumentStore


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ExplainConfig:
    """How the model explains anomalies: the provider, its prompt and the bounds of the talk."""

    provider: ToolProvider
    prompt: Prompt
    max_tokens: int = 1500
    max_tool_calls: int = 4
    max_turns: int = 5


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
    ledger: MCPServer | StdioServerParameters | None = None
    """Where to reach the ledger (a subprocess to start, or a server in the same process).
    Without it a run can be approved but not posted."""
    ledger_policy: CallPolicy = field(default_factory=CallPolicy)
    explain: ExplainConfig | None = None
    """Without it, anomalies are not explained and the run is otherwise the same."""
