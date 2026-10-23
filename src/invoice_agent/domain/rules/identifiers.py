"""Identifier and bank-detail checks."""

import re

from invoice_agent.domain.iban import contains_iban
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.anomaly import Anomaly, AnomalyCode
from invoice_agent.domain.rules.injection import document_texts, squash_text
from invoice_agent.domain.supplier import Supplier


def luhn_valid(number: str) -> bool:
    """Luhn checksum, used by French SIREN (9 digits) and SIRET (14 digits)."""
    if not number.isdigit():
        return False
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def check_siren(data: InvoiceData, supplier: Supplier | None) -> list[Anomaly]:
    """The seller SIREN must be well formed and, if a supplier was identified, be theirs.

    The second part matters because a supplier can be recognised through its VAT number
    even when the SIREN is wrong: a different but well-formed SIREN must not pass silently.
    One anomaly at most, whatever the reason.
    """
    siren = data.seller.siren
    if siren is None:
        return []
    if not luhn_valid(siren):
        reason = f"seller SIREN {siren} fails the Luhn check"
    elif supplier is not None and siren != supplier.siren:
        reason = f"seller SIREN {siren} differs from the one on file for {supplier.code}"
    else:
        return []
    return [Anomaly(code=AnomalyCode.INVALID_SIREN, message=reason, evidence={"siren": siren})]


# Notices announcing a change of bank details, matched on squashed text (see squash_text).
_BANK_CHANGE = re.compile(
    r"\bnouvel(le)?s?\s+coordonnees\s+bancaires\b"
    r"|\bnouveau\s+(rib|iban|compte)\b"
    r"|\b(changement|modification)\s+(de\s+|d\s+)?(nos\s+|vos\s+|notre\s+|votre\s+)?(banque|rib|iban|coordonnees|references)\b"
    r"|\bcoordonnees\s+bancaires\s+(ont\s+)?(change|modifie)"
    r"|\b(utiliser|regler|virer|verser|payer)\b.{0,30}\b(nouveau|nouvelle|nouveaux|nouvelles)\b"
    r".{0,20}\b(compte|rib|iban|coordonnees)\b"
    r"|\biban\s+a\s+utiliser\b"
    r"|\bnew\s+bank\s+(details|account)\b"
    r"|\b(bank|banking)\s+details\s+(have\s+)?changed\b"
    r"|\b(change|update)\s+(of\s+)?(your\s+)?(bank|payment)\s+(details|account|instructions)\b"
)


def check_iban(data: InvoiceData, supplier: Supplier | None) -> list[Anomaly]:
    """Payment details must be the ones on file, and nothing may try to redirect the payment.

    Two independent signals, reported as a single IBAN_MISMATCH:
    - the payment IBAN differs from the supplier's (nothing is raised if either is unknown);
    - any text of the invoice announces new bank details or contains an IBAN. Legitimate invoices
      carry their IBAN in the payment fields, not in free text; this also covers a supplier
      with no IBAN on file, or an unchanged IBAN next to a "pay elsewhere" notice.
    """
    reasons: list[str] = []
    evidence: dict[str, str] = {}
    on_file = supplier.iban if supplier is not None else None
    if on_file is not None and data.iban is not None and not data.iban.matches(on_file):
        reasons.append("payment IBAN differs from the one on file for this supplier")
        evidence["invoice_iban"] = data.iban.masked
        evidence["reference_iban"] = on_file.masked
    free_texts = document_texts(data)
    if any(_BANK_CHANGE.search(squash_text(text)) for text in free_texts):
        reasons.append("a note announces new bank details")
    if any(contains_iban(text) for text in free_texts):
        reasons.append("an IBAN is written in free text")
    if not reasons:
        return []
    return [
        Anomaly(
            code=AnomalyCode.IBAN_MISMATCH,
            message="; ".join(reasons),
            evidence=dict(evidence),
        )
    ]
