"""Tools to set the web interface up.

    uv run python -m invoice_agent.api hash-password   # asks for a password, prints its hash
    uv run python -m invoice_agent.api secret          # prints a random WEB_SECRET

The hash goes in ``APPROVERS`` as ``name=<hash>`` (several, separated by commas). The password
is read without echo on a terminal, or from standard input when piped; it is never accepted
on the command line, where the shell history would keep it.
"""

import argparse
import getpass
import secrets
import sys
from collections.abc import Sequence

from invoice_agent.api.auth import hash_password

MIN_PASSWORD_LENGTH = 12


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoice_agent.api")
    parser.add_argument("command", choices=["hash-password", "secret"])
    args = parser.parse_args(argv)
    if args.command == "secret":
        print(secrets.token_urlsafe(48))
        return 0
    password = (
        getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    )
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"the password must have at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
