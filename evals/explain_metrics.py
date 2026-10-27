"""Deterministic checks on an explanation: no second model acts as a judge.

What can be verified by code is verified by code: which anomalies are covered, whether the
model invented a code or a figure, whether it kept to the read-only tools and their budget,
and whether it gave a directive it was told not to give.
"""

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
DIRECTIVE = re.compile(
    r"\b(?:approuv\w*|valid(?:er|ez)\s+(?:la|cette)\s+facture|pay(?:er|ez)|rejet\w*|refus(?:er|ez))",
    re.IGNORECASE,
)
READ_ONLY_TOOLS = frozenset(
    {"get_supplier", "list_open_purchase_orders", "get_purchase_order", "find_invoices"}
)


def numbers_in(text: str) -> set[Decimal]:
    """The numbers written in a text, whether the decimal mark is a dot or a comma."""
    found: set[Decimal] = set()
    for token in NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", ".")).normalize())
        except InvalidOperation:
            continue
    return found


def ungrounded_figures(explanation: str, sources: Iterable[str]) -> list[str]:
    """Figures in the explanation that appear in none of the sources.

    A figure is an amount (it has decimals) or a number of at least 100. Small whole numbers
    ("two invoices", "10 %") are ordinary words and rates, not claims about this invoice.
    """
    allowed: set[Decimal] = set()
    for source in sources:
        allowed |= numbers_in(source)
    suspicious = []
    for token in NUMBER.findall(explanation):
        value = Decimal(token.replace(",", ".")).normalize()
        is_claim = "." in token or "," in token or value >= 100
        if is_claim and value not in allowed:
            suspicious.append(token)
    return suspicious


def coverage(given: Sequence[str], explained: Iterable[str]) -> float:
    """Share of the anomalies found by the rules that the explanation addresses."""
    if not given:
        return 1.0
    explained_set = set(explained)
    return sum(code in explained_set for code in given) / len(given)


def invented_codes(given: Iterable[str], explained: Iterable[str]) -> list[str]:
    """Codes the model wrote about that the rules did not report."""
    known = set(given)
    return sorted({code for code in explained if code not in known})


def tool_discipline(names: Sequence[str], *, budget: int = 4) -> bool:
    """Only read-only tools of the list, and within the budget."""
    return len(names) <= budget and all(name in READ_ONLY_TOOLS for name in names)


def directives(text: str) -> list[str]:
    """Phrases that tell the reviewer what to decide, which the model was told not to do."""
    return [match.group(0).lower() for match in DIRECTIVE.finditer(text)]
