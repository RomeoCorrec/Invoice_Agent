"""Run the mailroom.

uv run python -m mailroom seed                      # load the supplier contacts
uv run python -m mailroom                           # serve over stdio (an MCP client starts it)
uv run python -m mailroom --read-only               # serve without the send tool
uv run python -m mailroom deliver --thread <uuid> --from <address> --subject "..." --body "..."
                                                    # a reply arrives (the provider's side)
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from invoice_agent.observability.logging import configure_logging
from mailroom.schemas import MailroomRefusal
from mailroom.seed import seed_contacts
from mailroom.server import build_server
from mailroom.store import MailroomStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailroom")
    parser.add_argument("command", nargs="?", choices=["serve", "seed", "deliver"], default="serve")
    parser.add_argument("--read-only", action="store_true", help="do not expose the send tool")
    parser.add_argument("--thread", type=UUID, help="deliver: the conversation answered")
    parser.add_argument("--from", dest="sender", help="deliver: the address the reply comes from")
    parser.add_argument("--subject", default="Re:", help="deliver: the subject of the reply")
    parser.add_argument("--body", help="deliver: the text of the reply")
    parser.add_argument("--body-file", type=Path, help="deliver: read the text from this file")
    args = parser.parse_args(argv)

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    url = os.environ.get("MAILROOM_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        logging.getLogger("mailroom").error("MAILROOM_DATABASE_URL or DATABASE_URL must be set")
        return 2
    engine = create_engine(url, pool_pre_ping=True)
    try:
        store = MailroomStore(engine)
        if args.command == "seed":
            print(f"{seed_contacts(engine)} contacts loaded")
            return 0
        if args.command == "deliver":
            return _deliver(store, args)
        build_server(store, writable=not args.read_only).run("stdio")
    finally:
        engine.dispose()
    return 0


def _deliver(store: MailroomStore, args: argparse.Namespace) -> int:
    body = args.body if args.body is not None else None
    if args.body_file is not None:
        body = args.body_file.read_text(encoding="utf-8")
    if args.thread is None or not args.sender or body is None:
        print("deliver needs --thread, --from and --body (or --body-file)", file=sys.stderr)
        return 2
    try:
        message = store.receive_message(args.thread, args.sender, args.subject, body)
    except MailroomRefusal as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    flags = [] if message.from_contact else ["NOT from the contact on file"]
    if message.iban_redacted:
        flags.append("bank details removed")
    print(f"delivered {message.id}" + (f" ({'; '.join(flags)})" if flags else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
