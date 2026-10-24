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

from sqlalchemy.orm import Session

from invoice_agent.config import ConfigError, Settings, load_settings
from invoice_agent.db.repositories import JobRepository
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.ingestion.isolated import read_document_isolated
from invoice_agent.observability.logging import configure_logging, log_event
from invoice_agent.worker.handlers import HANDLERS, JobContext, ingest_job
from invoice_agent.worker.runner import Worker
from invoice_agent.worker.store import LocalDocumentStore

logger = logging.getLogger("invoice_agent.worker")


def document_names(root: Path) -> list[str]:
    """Relative names (posix style) of the PDFs under ``root``, sorted."""
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.pdf") if path.is_file()
    )


def build_worker(settings: Settings) -> Worker:
    root = settings.worker.document_root
    if root is None:
        raise ConfigError("DOCUMENT_ROOT is not set")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    store = LocalDocumentStore(root)
    reader = partial(read_document_isolated, timeout_s=settings.worker.job_timeout_s)

    def context_for(session: Session, now: datetime) -> JobContext:
        return JobContext(
            session=session,
            now=now,
            store=store,
            extractor=None,  # the LLM extractor arrives with step 04
            iban_key=settings.iban_hash_key,
            limits=settings.limits,
            max_attempts=settings.worker.max_attempts,
            reader=reader,
        )

    return Worker(
        factory,
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        handlers=HANDLERS,
        context_for=context_for,
        settings=settings.worker,
    )


def enqueue_documents(settings: Settings) -> int:
    root = settings.worker.document_root
    if root is None:
        raise ConfigError("DOCUMENT_ROOT is not set")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    now = datetime.now(UTC)
    names = document_names(root.resolve())
    with session_scope(factory) as session:
        repo = JobRepository(session)
        for name in names:
            repo.enqueue(ingest_job(name, now=now, max_attempts=settings.worker.max_attempts))
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
        worker = build_worker(settings)
    except ConfigError as exc:
        log_event(logger, "configuration_error", level=logging.ERROR, error=str(exc))
        return 2
    log_event(logger, "no_extractor_configured", level=logging.WARNING)
    worker.install_signal_handlers()
    worker.run_forever()
    return 0
