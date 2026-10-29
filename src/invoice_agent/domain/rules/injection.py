"""Heuristic detection of prompt injection in document text (French and English).

This is a cheap deterministic tripwire, not a guarantee: it catches the common phrasings
so such an invoice goes to a human. Text from documents is always treated as data; a hit
only adds an anomaly, it never changes how the text is processed.
"""

import re
import unicodedata
from collections.abc import Iterable

from invoice_agent.domain.iban import fold_text
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
            r"|\b(dear|hello|attention|note\s+to)\s+(the\s+)?(ai|llm)\b"
            r"|\b(dear|hello|attention|note\s+to)\s+(the\s+)?(ai\s+)?(assistant|agent|model)\b"
        ),
        # Tells the reader to use a tool or to skip a control: invoices have no business doing it.
        "tool_call_instruction": (
            r"\b(call|invoke|execute|trigger)\b.{0,30}\b(post_?\s?entry|tools?|functions?)\b"
            r"|\bapproved\s*=\s*true\b"
        ),
        "skip_control": (
            r"\b(skip|bypass|circumvent)\b.{0,30}\b(approval|review|validation|checks?|controls?)\b"
        ),
        # Tells the reader which account to pay, e.g. INV-21 ("l'IBAN a utiliser est ...").
        "payment_instruction": (
            r"\b(iban|rib|compte)\s+a\s+(utiliser|crediter)\b"
            r"|\b(use|pay)\s+(the\s+)?(following|this)\s+(iban|account)\b"
        ),
        "amount_override": (
            r"\b(doit|doivent)\s+etre\s+lus?\b|\bau\s+lieu\s+de\b.{0,20}\d|\bread\s+(it\s+)?as\b|\binstead\s+of\b.{0,20}\d"
        ),
    }.items()
}


def normalize_text(text: str) -> str:
    """Lower-case, accent-free, with invisible characters removed (NFKC first)."""
    stripped = unicodedata.normalize("NFKD", fold_text(text))
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def squash_text(text: str) -> str:
    """``normalize_text`` with every run of punctuation or spaces reduced to one space."""
    return re.sub(r"[^a-z0-9]+", " ", normalize_text(text)).strip()


def find_injection_patterns(texts: Iterable[str]) -> list[str]:
    """Names of the patterns matched anywhere in ``texts`` (sorted, without duplicates)."""
    hits: set[str] = set()
    for text in texts:
        variants = (normalize_text(text), squash_text(text))
        hits.update(
            name
            for name, pattern in _PATTERNS.items()
            if any(pattern.search(variant) for variant in variants)
        )
    return sorted(hits)


MAX_PASSAGE_LINES = 3
MAX_PASSAGES = 10
MAX_CONTINUATION_LINES = 5
_SENTENCE_END = (".", "!", "?")


def suspicious_passages(text: str) -> list[str]:
    """Passages of a document's raw text that contain injection phrasing.

    Found from the text alone, so they do not depend on what an extractor reports. A
    phrase can be wrapped over up to MAX_PASSAGE_LINES lines, and a hit line that does not
    end a sentence is followed to the line that ends it (at most MAX_CONTINUATION_LINES).
    Overlapping hits are merged. All passages are returned: callers decide what too many
    means (the pipeline refuses beyond MAX_PASSAGES instead of silently ignoring the rest).
    """
    lines = [line.strip() for line in fold_text(text).splitlines() if line.strip()]
    marked = [False] * len(lines)
    for start in range(len(lines)):
        for size in range(1, MAX_PASSAGE_LINES + 1):
            window = lines[start : start + size]
            # Minimal on both sides: it hits, and without its first line it would not.
            if find_injection_patterns([" ".join(window)]) and (
                size == 1 or not find_injection_patterns([" ".join(window[1:])])
            ):
                for index in range(start, start + len(window)):
                    marked[index] = True
                break
    extended = list(marked)
    for index, line in enumerate(lines):
        if not marked[index] or line.endswith(_SENTENCE_END):
            continue
        # The sentence goes on: follow it up to the line that ends it (bounded).
        for follower in range(index + 1, min(index + 1 + MAX_CONTINUATION_LINES, len(lines))):
            extended[follower] = True
            if lines[follower].endswith(_SENTENCE_END):
                break
    passages: list[str] = []
    current: list[str] = []
    for line, is_marked in zip(lines, extended, strict=True):
        if is_marked:
            current.append(line)
        elif current:
            passages.append(" ".join(current))
            current = []
    if current:
        passages.append(" ".join(current))
    return passages


def document_texts(data: InvoiceData) -> list[str]:
    """Every string in the invoice that a model could read, whatever the field.

    Collected generically from the serialised model, so a field added later (or a short one
    like the invoice number) is covered without touching this function.
    """
    texts: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    # The IBAN fingerprint is excluded: its hex digest is not text a model would obey.
    collect(data.model_dump(mode="json", exclude={"iban"}))
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
