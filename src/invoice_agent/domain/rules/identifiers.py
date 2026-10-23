"""Identifier and bank-detail checks."""

from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.anomaly import Anomaly, AnomalyCode
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


def check_siren(data: InvoiceData) -> list[Anomaly]:
    siren = data.seller.siren
    if siren is None or luhn_valid(siren):
        return []
    return [
        Anomaly(
            code=AnomalyCode.INVALID_SIREN,
            message=f"seller SIREN {siren} fails the Luhn check",
            evidence={"siren": siren},
        )
    ]


def check_iban(data: InvoiceData, supplier: Supplier | None) -> list[Anomaly]:
    """Payment IBAN must be the one on file. Nothing is raised if either side is unknown."""
    if supplier is None or supplier.iban is None or data.iban is None:
        return []
    if data.iban.matches(supplier.iban):
        return []
    return [
        Anomaly(
            code=AnomalyCode.IBAN_MISMATCH,
            message="payment IBAN differs from the one on file for this supplier",
            evidence={"invoice_iban": data.iban.masked, "reference_iban": supplier.iban.masked},
        )
    ]
