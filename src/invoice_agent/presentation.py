"""Showing text that came from a document to a person.

An invoice is untrusted input: its numbers and names reach the reviewer's screen. Each place
that shows them (the terminal, a web page) also escapes what its own medium interprets;
this is the part they share.
"""


def safe_text(value: object, limit: int = 60) -> str:
    """Text from a document, made safe to show: a reviewer's screen must not obey it.

    Control characters (escape sequences, carriage returns, newlines) and invisible format
    characters such as bidirectional overrides are replaced, so a hostile invoice cannot
    overwrite what is printed, fake a line of output, or make a number read backwards.
    """
    text = "".join(ch if ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in str(value))
    return text[:limit]


def safe_block(value: object, limit: int = 4000) -> str:
    """Like ``safe_text`` for a message of several lines: line breaks stay, nothing else
    that is not printable does."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch if ch == "\n" or ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in text
    )
    return text[:limit]
