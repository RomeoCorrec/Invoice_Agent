"""Command line: run a worker, or queue the documents found in the document root.

uv run python -m invoice_agent.worker            # run a worker until SIGINT/SIGTERM
uv run python -m invoice_agent.worker enqueue    # queue every PDF under DOCUMENT_ROOT
"""

import argparse
import logging
import os
import socket
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.build import build_graph
from invoice_agent.agent.graph.checkpoint import open_checkpointer
from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.run import Graph
from invoice_agent.config import ConfigError, Settings, load_settings
from invoice_agent.db.repositories import JobRepository
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.isolated import read_document_isolated
from invoice_agent.llm.factory import build_extractor
from invoice_agent.observability.logging import configure_logging, log_event
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.handlers import HANDLERS, JobContext, on_job_dead, process_job
from invoice_agent.worker.runner import Worker
from invoice_agent.worker.store import DocumentStore, LocalDocumentStore

logger = logging.getLogger("invoice_agent.worker")


def document_names(root: Path) -> list[str]:
    """Relative names (posix style) of the PDFs under ``root``, sorted."""
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.pdf") if path.is_file()
    )


def require_document_root(settings: Settings) -> Path:
    root = settings.worker.document_root
    if root is None:
        raise ConfigError("DOCUMENT_ROOT is not set")
    return root


def build_agent_graph(
    settings: Settings,
    factory: sessionmaker[Session],
    tracing: Tracing,
    extractor: InvoiceExtractor | None,
    saver: BaseCheckpointSaver[Any],
    store: DocumentStore | None = None,
) -> Graph:
    """The agent graph wired with the production dependencies (shared by the worker and the CLI).

    Without a ``store`` the documents come from ``DOCUMENT_ROOT``, which must then be set.
    """
    return build_graph(
        AgentDeps(
            session_factory=factory,
            store=store or LocalDocumentStore(require_document_root(settings)),
            extractor=extractor,
            iban_key=settings.iban_hash_key,
            limits=settings.limits,
            reader=partial(read_document_isolated, timeout_s=settings.worker.job_timeout_s),
            tracing=tracing,
        ),
        saver,
    )


def build_worker(
    settings: Settings,
    tracing: Tracing,
    extractor: InvoiceExtractor | None,
    saver: BaseCheckpointSaver[Any],
) -> Worker:
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    graph = build_agent_graph(settings, factory, tracing, extractor, saver)

    def context_for(session: Session, now: datetime) -> JobContext:
        return JobContext(session=session, now=now, tracing=tracing, agent=graph)

    return Worker(
        factory,
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        handlers=HANDLERS,
        context_for=context_for,
        settings=settings.worker,
        tracing=tracing,
        on_dead=on_job_dead,
    )


def enqueue_documents(settings: Settings) -> int:
    root = require_document_root(settings)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    now = datetime.now(UTC)
    names = document_names(root.resolve())
    with session_scope(factory) as session:
        repo = JobRepository(session)
        for name in names:
            repo.enqueue(process_job(name, now=now, max_attempts=settings.worker.max_attempts))
    engine.dispose()
    log_event(logger, "documents_enqueued", count=len(names))
    return len(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoice_agent.worker")
    parser.add_argument("command", nargs="?", choices=["run", "enqueue"], default="run")
    args = parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        settings = load_settings()
        if args.command == "enqueue":
            enqueue_documents(settings)
            return 0
        require_document_root(settings)  # before connecting to anything
        tracing = Tracing.from_env(os.environ)
        extractor = build_extractor(settings, tracing)
        log_event(
            logger,
            "worker_configured",
            llm_model=settings.llm.model if extractor else None,
            tracing=tracing.enabled,
        )
        if extractor is None:
            log_event(logger, "no_extractor_configured", level=logging.WARNING)
        with open_checkpointer(settings.database_url) as saver:
            worker = build_worker(settings, tracing, extractor, saver)
            worker.install_signal_handlers()
            try:
                worker.run_forever()
            finally:
                tracing.shutdown()  # sends what is still buffered to Langfuse
    except ConfigError as exc:
        log_event(logger, "configuration_error", level=logging.ERROR, error=str(exc))
        return 2
    return 0
