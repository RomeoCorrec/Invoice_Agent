"""Review the invoices the agent has paused on.

    uv run python -m invoice_agent.agent seed          # demo suppliers and purchase orders
    uv run python -m invoice_agent.agent pending
    uv run python -m invoice_agent.agent show <invoice-id>     # the entry that would be posted
    uv run python -m invoice_agent.agent approve <invoice-id> --by alice@example.com
    uv run python -m invoice_agent.agent reject <invoice-id> --by alice@example.com --comment "..."

A web page will replace this in a later step; the decision goes through the same function.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from invoice_agent.agent.graph.checkpoint import open_checkpointer
from invoice_agent.agent.graph.run import (
    CannotApproveError,
    NoPendingApprovalError,
    pending_approvals,
    pending_request,
    resume_run,
)
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict
from invoice_agent.config import ConfigError, load_settings
from invoice_agent.db.reference_data import load_reference_data
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.observability.logging import configure_logging
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.main import build_agent_graph
from invoice_agent.worker.store import NoDocumentStore

logger = logging.getLogger("invoice_agent.agent")


def safe_text(value: object, limit: int = 60) -> str:
    """Text from a document, made safe to print: the reviewer's terminal must not obey it.

    Control characters (escape sequences, carriage returns, newlines) and invisible format
    characters such as bidirectional overrides are replaced, so a hostile invoice cannot
    overwrite what the command prints or fake a line of output.
    """
    text = "".join(ch if ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in str(value))
    return text[:limit]


DEMO_REFERENCE = Path(__file__).parents[3] / "tests" / "fixtures" / "invoice-test-set" / "reference"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoice_agent.agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending", help="list the invoices waiting for a decision")
    show = commands.add_parser("show", help="show what a waiting invoice would post")
    show.add_argument("invoice_id", type=UUID)
    seed = commands.add_parser("seed", help="load the demo suppliers and purchase orders")
    seed.add_argument("--from", dest="source", type=Path, default=DEMO_REFERENCE)
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
            with open_checkpointer(settings.database_url) as saver:
                graph = build_agent_graph(
                    settings, factory, Tracing(), None, saver, NoDocumentStore()
                )
                _print_pending(pending_approvals(graph, factory))
            return 0
        if args.command == "show":
            with open_checkpointer(settings.database_url) as saver:
                graph = build_agent_graph(
                    settings, factory, Tracing(), None, saver, NoDocumentStore()
                )
                request = pending_request(graph, factory, args.invoice_id)
            if request is None:
                print(
                    f"no run is waiting for a decision on invoice {args.invoice_id}",
                    file=sys.stderr,
                )
                return 1
            print(render_request(request))
            return 0
        if args.command == "seed":
            with session_scope(factory) as session:
                loaded = load_reference_data(session, args.source, settings.iban_hash_key)
            print(f"loaded {len(loaded)} suppliers and their purchase orders")
            return 0
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
            graph = build_agent_graph(
                settings, factory, Tracing.from_env(os.environ), None, saver, NoDocumentStore()
            )
            try:
                result = resume_run(graph, factory, args.invoice_id, decision)
            except (NoPendingApprovalError, CannotApproveError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
        print(f"{args.invoice_id}: {result.state.get('status')}")
        return 0
    finally:
        engine.dispose()


def render_request(request: dict[str, Any]) -> str:
    """The details of a waiting invoice, with every document-derived text made safe."""
    lines = [
        f"invoice   {request['invoice_id']}  ({safe_text(request['status'], 12)})",
        f"number    {safe_text(request['invoice_number'], 40)}",
        f"supplier  {safe_text(request['seller'], 60)}",
        f"amounts   net {request['total_ht']}  vat {request['total_vat']}  "
        f"gross {request['total_ttc']} {safe_text(request['currency'], 3)}",
        "anomalies " + (safe_text(", ".join(request["anomaly_codes"]), 200) or "none"),
    ]
    entry, blocked = request.get("entry"), request.get("entry_blocked")
    if entry:
        lines.append(f"entry     {safe_text(entry['journal'], 8)} {entry['entry_date']}")
        for line in entry["lines"]:
            lines.append(
                f"  {safe_text(line['account'], 12):<10} {line['side']:<7} "
                f"{line['amount']:>10}  {safe_text(line['label'], 70)}"
            )
    elif blocked:
        lines.append(
            f"entry     none: {safe_text(blocked['reason'], 30)}: "
            f"{safe_text(blocked['detail'], 100)} (it can only be rejected)"
        )
    return "\n".join(lines)


def _print_pending(requests: list[dict[str, Any]]) -> None:
    if not requests:
        print("nothing is waiting for a decision")
    for request in requests:
        codes = ", ".join(str(code) for code in request["anomaly_codes"])
        print(
            f"{request['invoice_id']}  {safe_text(request['status'], 12):<12} "
            f"{safe_text(request['invoice_number'], 16):<16} "
            f"{safe_text(request['seller'], 28):<28} "
            f"{request['total_ttc']:>10} {safe_text(request['currency'], 3)}"
            + (f"  [{safe_text(codes, 120)}]" if codes else "")
        )


if __name__ == "__main__":
    raise SystemExit(main())
