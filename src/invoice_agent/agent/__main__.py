"""Review the invoices the agent has paused on.

    uv run python -m invoice_agent.agent pending
    uv run python -m invoice_agent.agent approve <invoice-id> --by alice@example.com
    uv run python -m invoice_agent.agent reject <invoice-id> --by alice@example.com --comment "..."

A web page will replace this in a later step; the decision goes through the same function.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.graph.checkpoint import open_checkpointer
from invoice_agent.agent.graph.run import NoPendingApprovalError, resume_run
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict
from invoice_agent.config import ConfigError, load_settings
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.observability.logging import configure_logging
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.main import build_agent_graph

logger = logging.getLogger("invoice_agent.agent")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoice_agent.agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending", help="list the invoices waiting for a decision")
    for name in ("approve", "reject"):
        command = commands.add_parser(name, help=f"{name} an invoice")
        command.add_argument("invoice_id", type=UUID)
        command.add_argument("--by", required=True, help="who is deciding")
        command.add_argument("--comment")
    args = parser.parse_args(argv)

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), stream=sys.stderr)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        if args.command == "pending":
            return _pending(factory)
        try:
            decision = ApprovalDecision(
                verdict=Verdict.APPROVE if args.command == "approve" else Verdict.REJECT,
                approver=args.by,
                comment=args.comment,
            )
        except ValidationError as exc:
            print(
                f"invalid decision: {exc.errors()[0]['loc'][0]} {exc.errors()[0]['msg']}",
                file=sys.stderr,
            )
            return 2
        with open_checkpointer(settings.database_url) as saver:
            graph = build_agent_graph(settings, factory, Tracing.from_env(os.environ), None, saver)
            try:
                result = resume_run(graph, factory, args.invoice_id, decision)
            except NoPendingApprovalError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        print(f"{args.invoice_id}: {result.state.get('status')}")
        return 0
    finally:
        engine.dispose()


def _pending(factory: sessionmaker[Session]) -> int:
    waiting = (InvoiceStatus.VALIDATED, InvoiceStatus.NEEDS_REVIEW)
    with session_scope(factory) as session:
        invoices = [
            i for status in waiting for i in InvoiceRepository(session).list_by_status(status)
        ]
        if not invoices:
            print("nothing is waiting for a decision")
        for invoice in invoices:
            data = invoice.data
            assert data is not None
            codes = _anomaly_codes(session, invoice.id)
            print(
                f"{invoice.id}  {invoice.status.value:<12} {data.invoice_number:<16} "
                f"{data.seller.name[:28]:<28} {data.total_ttc:>10} {data.currency}"
                + (f"  [{', '.join(codes)}]" if codes else "")
            )
    return 0


def _anomaly_codes(session: Session, invoice_id: UUID) -> list[str]:
    for event in AuditEventRepository(session).list_for_invoice(invoice_id):
        if event.step == "validate":
            anomalies = event.payload.get("anomalies", [])
            if isinstance(anomalies, list):
                return [str(a["code"]) for a in anomalies if isinstance(a, dict) and "code" in a]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
