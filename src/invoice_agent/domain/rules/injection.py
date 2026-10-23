"""Heuristic detection of prompt injection in document text (French and English).

This is a cheap deterministic tripwire, not a guarantee: it catches the common phrasings
so such an invoice goes to a human. Text from documents is always treated as data; a hit
only adds an anomaly, it never changes how the text is processed.
"""

import re
import unicodedata
from collections.abc import Iterable

from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.anomaly import Anomaly, AnomalyCode

# Matched against lower-cased text with accents removed.
_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "ignore_instructions_fr": (
            r"\bignore[rz]?\b.{0,40}\b(regles?|instructions?|consignes?|controles?|verifications?)\b"
        ),
        "ignore_instructions_en": (
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(rules?|instructions?|checks?|prompts?)\b"
        ),
        "system_instruction": (
            r"\b(instructions?|messages?|prompt)\s+(systeme|system)\b|\bsystem\s+prompt\b"
        ),
        "role_marker": r"(^|\n)\s*(system|assistant|developer)\s*:",
        "approve_and_post": r"\bapprouve\w*\b.{0,60}\bposte\w*\b|\bapprove\b.{0,60}\bpost\b",
        "skip_human_review": (
            r"sans\s+(revue|validation|controle)\s+humain\w*|without\s+(human\s+)?(review|approval)"
        ),
        "suppress_alerts": (
            r"ne\s+signale\s+(aucune|pas)|do\s+not\s+(flag|report)|(no|any)\s+anomal\w+\s+(should|must)"
        ),
        "address_the_ai": (
            r"\b(assistant|agent)\s+ia\b|\bnote\s+pour\s+l.?(assistant|ia)\b|\bfor\s+the\s+(ai|assistant|model)\b"
        ),
        "amount_override": (
            r"\b(doit|doivent)\s+etre\s+lus?\b|\bau\s+lieu\s+de\b.{0,20}\d|\bread\s+(it\s+)?as\b|\binstead\s+of\b.{0,20}\d"
        ),
        "payment_redirect": (
            r"\biban\s+a\s+utiliser\b|\bnouvel(le)?s?\s+coordonnees\s+bancaires\b|\bnew\s+bank\s+details\b"
        ),
    }.items()
}


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def find_injection_patterns(texts: Iterable[str]) -> list[str]:
    """Names of the patterns matched anywhere in ``texts`` (sorted, without duplicates)."""
    hits: set[str] = set()
    for text in texts:
        normalized = _normalize(text)
        hits.update(name for name, pattern in _PATTERNS.items() if pattern.search(normalized))
    return sorted(hits)


def document_texts(data: InvoiceData) -> list[str]:
    """Every free-text field of the invoice that a model could read."""
    texts = [line.description for line in data.lines]
    texts += [data.seller.name, *data.notes]
    if data.buyer:
        texts.append(data.buyer.name)
    if data.purchase_order_ref:
        texts.append(data.purchase_order_ref)
    return texts


def check_injection(data: InvoiceData) -> list[Anomaly]:
    hits = find_injection_patterns(document_texts(data))
    if not hits:
        return []
    return [
        Anomaly(
            code=AnomalyCode.PROMPT_INJECTION_SUSPECTED,
            message="document text contains instructions aimed at an AI system",
            evidence={"patterns": list(hits)},
        )
    ]
