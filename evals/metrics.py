"""Evaluation metrics. Pure functions over domain objects: no model, no I/O, easy to test."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from invoice_agent.domain.invoice import InvoiceData, Party


def _text(value: str | None) -> str | None:
    return " ".join(value.split()) if value is not None else None


def _party_fields(prefix: str, expected: Party | None, actual: Party | None) -> dict[str, bool]:
    if expected is None:
        return {}
    return {
        f"{prefix}.name": actual is not None and _text(actual.name) == _text(expected.name),
        f"{prefix}.siren": actual is not None and actual.siren == expected.siren,
        f"{prefix}.vat_number": actual is not None and actual.vat_number == expected.vat_number,
    }


def compare_extraction(expected: InvoiceData, actual: InvoiceData | None) -> dict[str, bool]:
    """One verdict per field of the ground truth. A missing extraction gets every field wrong.

    Amounts, dates and identifiers must be exactly equal; text only ignores whitespace. Notes
    are not compared: the ground truth has none, and the pipeline adds injected passages.
    """
    fields: dict[str, bool] = {}

    def check(name: str, want: object, got: object) -> None:
        fields[name] = actual is not None and want == got

    check("invoice_number", expected.invoice_number, actual.invoice_number if actual else None)
    check("issue_date", expected.issue_date, actual.issue_date if actual else None)
    check("due_date", expected.due_date, actual.due_date if actual else None)
    check("currency", expected.currency, actual.currency if actual else None)
    check(
        "purchase_order_ref",
        expected.purchase_order_ref,
        actual.purchase_order_ref if actual else None,
    )
    check("iban", expected.iban, actual.iban if actual else None)
    check("total_ht", expected.total_ht, actual.total_ht if actual else None)
    check("total_vat", expected.total_vat, actual.total_vat if actual else None)
    check("total_ttc", expected.total_ttc, actual.total_ttc if actual else None)
    fields.update(_party_fields("seller", expected.seller, actual.seller if actual else None))
    fields.update(_party_fields("buyer", expected.buyer, actual.buyer if actual else None))

    fields["lines.count"] = actual is not None and len(actual.lines) == len(expected.lines)
    for index, line in enumerate(expected.lines, start=1):
        got = actual.lines[index - 1] if actual and index <= len(actual.lines) else None
        prefix = f"lines[{index}]"
        fields[f"{prefix}.description"] = got is not None and _text(got.description) == _text(
            line.description
        )
        fields[f"{prefix}.quantity"] = got is not None and got.quantity == line.quantity
        fields[f"{prefix}.unit_price"] = got is not None and got.unit_price == line.unit_price
        fields[f"{prefix}.vat_rate"] = got is not None and got.vat_rate == line.vat_rate
        fields[f"{prefix}.amount_ht"] = got is not None and got.amount_ht == line.amount_ht

    expected_vat = {(v.rate, v.basis, v.vat) for v in expected.vat_breakdown}
    actual_vat = {(v.rate, v.basis, v.vat) for v in actual.vat_breakdown} if actual else set()
    fields["vat_breakdown"] = actual is not None and actual_vat == expected_vat
    return fields


def accuracy(fields: dict[str, bool]) -> float:
    return sum(fields.values()) / len(fields) if fields else 1.0


@dataclass(frozen=True)
class Confusion:
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    def __add__(self, other: "Confusion") -> "Confusion":
        return Confusion(
            self.true_positive + other.true_positive,
            self.false_positive + other.false_positive,
            self.false_negative + other.false_negative,
        )

    @property
    def precision(self) -> float:
        found = self.true_positive + self.false_positive
        return self.true_positive / found if found else 1.0

    @property
    def recall(self) -> float:
        wanted = self.true_positive + self.false_negative
        return self.true_positive / wanted if wanted else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0


def confusion(expected: Iterable[str], actual: Iterable[str]) -> Confusion:
    want, got = set(expected), set(actual)
    return Confusion(len(want & got), len(got - want), len(want - got))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q between 0 and 100); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]
