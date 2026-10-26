"""Run the ledger.

uv run python -m ledger seed        # load the reference data
uv run python -m ledger             # serve over stdio (an MCP client starts this itself)
uv run python -m ledger --read-only # serve without the write tool
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine

from invoice_agent.observability.logging import configure_logging
from ledger.seed import seed_reference
from ledger.server import build_server
from ledger.store import LedgerStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger")
    parser.add_argument("command", nargs="?", choices=["serve", "seed"], default="serve")
    parser.add_argument("--read-only", action="store_true", help="do not expose the write tool")
    args = parser.parse_args(argv)

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    url = os.environ.get("LEDGER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        logging.getLogger("ledger").error("LEDGER_DATABASE_URL or DATABASE_URL must be set")
        return 2
    engine = create_engine(url, pool_pre_ping=True)
    try:
        if args.command == "seed":
            seed_reference(engine)
            return 0
        build_server(LedgerStore(engine), writable=not args.read_only).run("stdio")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
