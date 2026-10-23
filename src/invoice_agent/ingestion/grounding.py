"""Check that what an extractor returned is really printed in the document.

An LLM can be steered by the document it reads (prompt injection) or simply hallucinate.
Every identifier, name, date, IBAN and amount in the result must therefore appear in the
text it was extracted from; otherwise the extraction is rejected (fail closed).

- Amounts are compared as numbers (``2 200,00`` is ``2200.00``), read as whole numeric
  tokens. IBANs and long digit runs (SIREN, SIRET) are removed first, so a group of digits
  of an IBAN cannot pass for an amount.
- Identifiers must appear contiguously; names, descriptions and notes as words in order,
  at most MAX_WORD_GAP words apart (table cells of neighbouring columns can be interleaved).
- Dates must be whole tokens, not part of a longer number.
- Passages the *pipeline* found to contain injection phrasing are declared as notes before
  this check and removed from the text first: a figure that only occurs inside an injected
  passage ("read the amount as 64,00") does not count as printed on the invoice. What is
  excluded is decided from the text, never by the extractor.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from invoice_agent.domain.iban import (
    IbanFingerprint,
    fold_text,
    hash_iban,
    iter_ibans,
    redact_ibans,
)
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.injection import squash_text
from invoice_agent.ingestion.document import ExtractionError

MAX_WORD_GAP = 12
MAX_INTEGER_DIGITS = 8
"""Longer digit runs are identifiers (a SIREN has 9 digits), not amounts: this caps a
single figure at 99,999,999 euros, far above any invoice this system handles."""

_REMOVED = "\n|\n"
_SPACE = " \u00a0\u202f"
# French grouping ("1 250,00"), otherwise a plain number with optional separators.
_NUMBER_TOKEN = re.compile(
    rf"(?<![\d.,])(?:\d{{1,3}}(?:[{_SPACE}]\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)(?![\d])"
)


def _readings(token: str) -> set[Decimal]:
    """The values a printed number token can denote (``2,200`` is 2200 or 2.200)."""
    compact = re.sub(f"[{_SPACE}]", "", token)
    commas, dots = compact.count(","), compact.count(".")
    candidates: set[str] = set()
    if commas and dots:
        last = max(compact.rfind(","), compact.rfind("."))
        candidates.add(re.sub(r"[.,]", "", compact[:last]) + "." + compact[last + 1 :])
    elif commas + dots == 0:
        candidates.add(compact)
    else:
        separator = "," if commas else "."
        if commas + dots > 1:
            candidates.add(compact.replace(separator, ""))  # only thousands marks
        else:
            head, _, tail = compact.partition(separator)
            candidates.add(f"{head}.{tail}")  # always possible as a decimal
            if len(tail) == 3:
                candidates.add(head + tail)  # or a thousands mark: 2,200
    values: set[Decimal] = set()
    for candidate in candidates:
        integer_part = candidate.partition(".")[0]
        if len(integer_part) > MAX_INTEGER_DIGITS:
            continue
        try:
            values.add(Decimal(candidate))
        except InvalidOperation:
            continue
    return values


def _numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER_TOKEN.findall(text):
        found.update(_readings(token))
    return found


def _date_printed(value: date, text: str) -> bool:
    renderings = (
        value.strftime("%d/%m/%Y"),
        value.strftime("%d.%m.%Y"),
        value.strftime("%d-%m-%Y"),
        value.strftime("%Y-%m-%d"),
        value.strftime("%d/%m/%y"),
    )
    return any(
        re.search(rf"(?<![\d/.\-]){re.escape(rendering)}(?![\d])", text) for rendering in renderings
    )


def _remove_passages(text: str, passages: Iterable[str]) -> str:
    """``text`` without each passage, however the PDF wrapped it across lines."""
    for passage in passages:
        words = fold_text(passage).split()
        if words:
            body = r"\s+".join(re.escape(word) for word in words)
            # Whole words only, and a separator that cannot glue two digit runs together.
            text = re.sub(rf"(?<!\w){body}(?!\w)", _REMOVED, text, flags=re.IGNORECASE)
    return text


def _contiguous(value: str, squashed_text: str) -> bool:
    """Identifiers must appear as one run of tokens, with nothing glued to its ends."""
    needle = squash_text(value)
    return bool(needle) and f" {needle} " in f" {squashed_text} "


def _in_order(value: str, words: list[str]) -> bool:
    """The words of ``value`` appear in ``words`` in order, at most MAX_WORD_GAP apart.

    Other words may sit in between: in a PDF table the cells of neighbouring columns are
    interleaved with a long wrapped description. An invented or altered value still fails.
    """
    needle = squash_text(value).split()
    if not needle:
        return False  # nothing but punctuation: there is nothing to verify
    for start, word in enumerate(words):
        if word != needle[0]:
            continue
        position, matched = start, 1
        while matched < len(needle):
            window = words[position + 1 : position + 2 + MAX_WORD_GAP]
            try:
                position = position + 1 + window.index(needle[matched])
            except ValueError:
                break
            matched += 1
        if matched == len(needle):
            return True
    return False


def verify_grounded(
    data: InvoiceData, text: str, iban_key: bytes, *, excluded: Sequence[str] = ()
) -> None:
    """Raise ExtractionError, naming the offending fields, if ``data`` is not in ``text``.

    ``excluded`` are the passages that *our own scan* found to hold injection phrasing
    (``suspicious_passages``): they do not count as printed. The notes the extractor
    reports play no part in that decision; they are only checked to exist in the text.
    """
    folded = fold_text(text)
    printed = _remove_passages(folded, excluded)
    squashed = squash_text(printed)
    words = squashed.split()
    # IBANs are masked on the original text, before anything is cut out of it.
    numbers = _numbers_in(_remove_passages(redact_ibans(folded), excluded))
    missing: list[str] = []

    def has_id(field: str, value: str | None) -> None:
        if value and not _contiguous(value, squashed):
            missing.append(field)

    def has_words(field: str, value: str | None) -> None:
        if value and not _in_order(value, words):
            missing.append(field)

    def has_number(field: str, value: Decimal) -> None:
        if value not in numbers:
            missing.append(field)

    has_id("invoice_number", data.invoice_number)
    has_words("seller.name", data.seller.name)
    has_id("seller.siren", data.seller.siren)
    has_id("seller.vat_number", data.seller.vat_number)
    if data.buyer:
        has_words("buyer.name", data.buyer.name)
        has_id("buyer.siren", data.buyer.siren)
        has_id("buyer.vat_number", data.buyer.vat_number)
    has_id("purchase_order_ref", data.purchase_order_ref)

    for field, value in {"issue_date": data.issue_date, "due_date": data.due_date}.items():
        if value is not None and not _date_printed(value, printed):
            missing.append(field)

    for index, line in enumerate(data.lines, start=1):
        has_words(f"lines[{index}].description", line.description)
        has_number(f"lines[{index}].quantity", line.quantity)
        has_number(f"lines[{index}].unit_price", line.unit_price)
        has_number(f"lines[{index}].vat_rate", line.vat_rate)
        has_number(f"lines[{index}].amount_ht", line.amount_ht)
    for index, vat in enumerate(data.vat_breakdown, start=1):
        has_number(f"vat_breakdown[{index}].rate", vat.rate)
        has_number(f"vat_breakdown[{index}].basis", vat.basis)
        has_number(f"vat_breakdown[{index}].vat", vat.vat)
    has_number("total_ht", data.total_ht)
    has_number("total_vat", data.total_vat)
    has_number("total_ttc", data.total_ttc)

    if data.iban is not None and not _iban_is_printed(data.iban, printed, iban_key):
        missing.append("iban")
    all_words = squash_text(folded).split()
    for index, note in enumerate(data.notes, start=1):
        if not _in_order(note, all_words):
            missing.append(f"notes[{index}]")

    if missing:
        raise ExtractionError("extracted values not found in the document: " + ", ".join(missing))


def _iban_is_printed(fingerprint: IbanFingerprint, printed: str, key: bytes) -> bool:
    return any(hash_iban(iban, key) == fingerprint.digest for iban in iter_ibans(printed))
