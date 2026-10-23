"""Helpers for duplicate detection."""

import re

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def normalize_invoice_number(number: str) -> str:
    """``F-001``, ``f 001`` and ``F001`` are the same invoice number."""
    return _NON_ALNUM.sub("", number).upper()
