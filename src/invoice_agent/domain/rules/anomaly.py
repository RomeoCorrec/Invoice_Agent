"""Anomalies raised by the deterministic checks."""

from enum import StrEnum

from pydantic import Field, JsonValue

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.types import NonEmptyStr


class AnomalyCode(StrEnum):
    # Declaration order is the order anomalies are reported in.
    VAT_AMOUNT_MISMATCH = "VAT_AMOUNT_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    UNKNOWN_SUPPLIER = "UNKNOWN_SUPPLIER"
    INVALID_SIREN = "INVALID_SIREN"
    IBAN_MISMATCH = "IBAN_MISMATCH"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    NO_PO_MATCH = "NO_PO_MATCH"
    AMOUNT_EXCEEDS_PO = "AMOUNT_EXCEEDS_PO"
    PO_ALREADY_INVOICED = "PO_ALREADY_INVOICED"
    PROMPT_INJECTION_SUSPECTED = "PROMPT_INJECTION_SUSPECTED"


class Anomaly(DomainModel):
    code: AnomalyCode
    message: NonEmptyStr
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    """JSON-safe facts behind the anomaly (amounts as strings), for the reviewer and the audit."""


def sort_anomalies(anomalies: list[Anomaly]) -> tuple[Anomaly, ...]:
    order = list(AnomalyCode)
    return tuple(sorted(anomalies, key=lambda anomaly: order.index(anomaly.code)))


def recommended_status(anomalies: tuple[Anomaly, ...]) -> InvoiceStatus:
    """No anomaly: ready for the human approval. Otherwise: a human must review first."""
    return InvoiceStatus.NEEDS_REVIEW if anomalies else InvoiceStatus.VALIDATED
