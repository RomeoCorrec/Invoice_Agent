"""Validate that a commit message follows the Conventional Commits format.

Used as a pre-commit ``commit-msg`` hook: the first argument is the path of the
file holding the message.
"""

import re
import sys
from pathlib import Path

TYPES = ("feat", "fix", "test", "docs", "refactor", "chore", "build", "ci", "perf", "style")
MAX_SUBJECT_LENGTH = 72

_PATTERN = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9._/-]+\))?!?: \S.*$")
_IGNORED_PREFIXES = ("Merge ", "fixup! ", "squash! ")


def check(message: str) -> str | None:
    """Return an error description, or None if the message is valid."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    subject = lines[0].strip() if lines else ""
    if subject.startswith(_IGNORED_PREFIXES):
        return None
    if not _PATTERN.match(subject):
        return f"subject must look like 'type(scope): description' with type in {TYPES}"
    if len(subject) > MAX_SUBJECT_LENGTH:
        return f"subject is {len(subject)} characters, the limit is {MAX_SUBJECT_LENGTH}"
    if len(lines) > 1 and lines[1].strip():
        return "leave a blank line between the subject and the body"
    return None


def main(argv: list[str]) -> int:
    error = check(Path(argv[1]).read_text(encoding="utf-8"))
    if error:
        print(f"Invalid commit message: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
