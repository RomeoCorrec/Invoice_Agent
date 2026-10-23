"""Check that what an extractor returned is really printed in the document.

An LLM can be steered by the document it reads (prompt injection) or simply hallucinate.
Every identifier, name, date, IBAN and amount in the result must therefore appear in the
text it was extracted from; otherwise the extraction is rejected (fail closed).

Amounts are compared as numbers, so ``2 200,00`` and ``2200.00`` are the same value. The
notes declared by the extractor are removed from the text first: a number that only
occurs inside a note ("read the amount as 64,00") does not count as printed on the invoice.
"""

import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation

from invoice_agent.domain.iban import IbanFingerprint, fold_text, hash_iban, iter_ibans
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.injection import squash_text
from invoice_agent.ingestion.document import ExtractionError

_NUMBER_RUN = re.compile(r"\d[\d \u00a0\u202f.,]*")
_SPACES = re.compile(r"[ \u00a0\u202f]+")


def _numbers_in(text: str) -> set[Decimal]:
    """Every number that could be printed in ``text``, under both readings of ``,`` and ``.``."""
    found: set[Decimal] = set()
    for run in _NUMBER_RUN.findall(text):
        run = run.strip(" \u00a0\u202f.,")
        pieces = [run, *_SPACES.split(run)]
        for piece in pieces:
            found.update(_readings(piece))
    return found


def _readings(token: str) -> Iterable[Decimal]:
    compact = _SPACES.sub("", token)
    if not compact:
        return
    candidates = {compact.replace(",", "").replace(".", "")}  # separators were thousands marks
    for separator in (",", "."):
        head, found, tail = compact.rpartition(separator)
        if found and tail.isdigit():
            candidates.add(head.replace(",", "").replace(".", "") + "." + tail)
    for candidate in candidates:
        try:
            yield Decimal(candidate)
        except InvalidOperation:
            continue


def _date_renderings(value: date) -> set[str]:
    return {
        value.strftime("%d/%m/%Y"),
        value.strftime("%d.%m.%Y"),
        value.strftime("%d-%m-%Y"),
        value.strftime("%Y-%m-%d"),
        value.strftime("%d/%m/%y"),
    }


def _without(text: str, notes: Iterable[str]) -> str:
    """``text`` with each note removed, however the PDF wrapped it across lines."""
    for note in notes:
        words = note.split()
        if words:
            pattern = r"\s+".join(re.escape(word) for word in words)
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def _in_order(value: str, haystack: list[str]) -> bool:
    """True if the words of ``value`` appear in ``haystack`` in the same order.

    Other words may sit in between: in a PDF table the cells of neighbouring columns are
    interleaved with a long wrapped description. An invented or altered value still fails.
    """
    needle = squash_text(value).split()
    remaining = iter(haystack)
    return all(word in remaining for word in needle)


def verify_grounded(data: InvoiceData, text: str, iban_key: bytes) -> None:
    """Raise ExtractionError, naming the offending fields, if ``data`` is not in ``text``."""
    printed = _without(text, data.notes)
    folded = fold_text(printed)
    words = squash_text(printed).split()
    numbers = _numbers_in(folded)
    missing: list[str] = []

    def has_text(field: str, value: str | None) -> None:
        if value and not _in_order(value, words):
            missing.append(field)

    def has_number(field: str, value: Decimal) -> None:
        if value not in numbers:
            missing.append(field)

    has_text("invoice_number", data.invoice_number)
    has_text("seller.name", data.seller.name)
    has_text("seller.siren", data.seller.siren)
    has_text("seller.vat_number", data.seller.vat_number)
    if data.buyer:
        has_text("buyer.name", data.buyer.name)
        has_text("buyer.siren", data.buyer.siren)
        has_text("buyer.vat_number", data.buyer.vat_number)
    has_text("purchase_order_ref", data.purchase_order_ref)

    dates = {"issue_date": data.issue_date, "due_date": data.due_date}
    for field, value in dates.items():
        if value is not None and not any(r in folded for r in _date_renderings(value)):
            missing.append(field)

    for index, line in enumerate(data.lines, start=1):
        has_text(f"lines[{index}].description", line.description)
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

    if data.iban is not None and not _iban_is_printed(data.iban, folded, iban_key):
        missing.append("iban")
    all_words = squash_text(text).split()
    for index, note in enumerate(data.notes, start=1):
        if not _in_order(note, all_words):
            missing.append(f"notes[{index}]")

    if missing:
        raise ExtractionError("extracted values not found in the document: " + ", ".join(missing))


def _iban_is_printed(fingerprint: IbanFingerprint, folded_text: str, key: bytes) -> bool:
    return any(hash_iban(iban, key) == fingerprint.digest for iban in iter_ibans(folded_text))
