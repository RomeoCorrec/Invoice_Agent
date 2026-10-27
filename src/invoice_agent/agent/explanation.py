"""What the model is asked about an invoice with anomalies, and what is kept of its answer.

The model gets numbers, references and the anomalies the rules found, never a string from the
document (names, numbers, notes, descriptions). Its answer is advisory text for the reviewer: it
does not change the status, the entry or the decision, and a code it was not given is dropped.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from invoice_agent.domain.invoice import InvoiceData

CASE_TAG = re.compile(r"<\s*/?\s*case\b[^>]*>", re.IGNORECASE)
SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:/-]{1,40}")
"""An identifier: no space, so no sentence fits in it."""
SAFE_MESSAGE = re.compile(r"[A-Za-z0-9 .,;:%()=/+'-]{1,400}")
"""The characters of the messages our own rules write (words, numbers, rates, comparisons)."""
NEUTRAL_MESSAGES = {
    "UNKNOWN_SUPPLIER": "the seller is not in the supplier reference data",
    "DUPLICATE_INVOICE": "an invoice with the same number was already received",
    "PO_NOT_FOUND": "the purchase order reference does not exist for this supplier",
}
"""Rules whose message quotes the document (seller name, invoice number, order reference): the
message is never sent, this text is sent instead."""
PASS_THROUGH_MESSAGES = frozenset(
    {
        "VAT_AMOUNT_MISMATCH",  # rates and amounts
        "TOTAL_MISMATCH",  # amounts
        "INVALID_SIREN",  # nine validated digits, or a supplier code of ours
        "IBAN_MISMATCH",  # fixed reasons
        "NO_PO_MATCH",  # fixed text
        "AMOUNT_EXCEEDS_PO",  # an order number from our reference data, amounts
        "PO_ALREADY_INVOICED",  # an order number from our reference data
        "PROMPT_INJECTION_SUSPECTED",  # fixed text
    }
)
"""Rules whose message was checked to be made of our own text and numbers. Anything not listed
here or above gets no message: a new rule has to be classified before its text can be sent."""
MAX_CODES = 20


class AnomalyExplanation(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    explanation: str = Field(min_length=1, max_length=600)


class Explanation(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    items: list[AnomalyExplanation] = Field(max_length=MAX_CODES)


@dataclass(frozen=True)
class Checked:
    """The explanation limited to the anomalies that exist, and how much was dropped."""

    explanation: Explanation
    dropped: int


def build_case(data: InvoiceData, anomalies: list[dict[str, Any]]) -> str:
    """The case file the model reads: numbers, references and findings as JSON in <case> tags.

    No string from the document reaches the model: not the seller's name, not the invoice
    number, not the notes or the descriptions. What is left is digits (SIREN, amounts, rates),
    a purchase order reference when it has the shape of an identifier, codes and our own
    messages. A hostile document therefore has no sentence to plant in the prompt.
    """
    case = {
        "invoice": {
            "seller_siren": data.seller.siren,
            "currency": data.currency,
            "total_ht": str(data.total_ht),
            "total_vat": str(data.total_vat),
            "total_ttc": str(data.total_ttc),
            "vat_breakdown": [
                {"rate": str(v.rate), "basis": str(v.basis), "vat": str(v.vat)}
                for v in data.vat_breakdown
            ],
            "purchase_order_ref": _token(data.purchase_order_ref),
        },
        "anomalies": [
            {
                "code": a["code"],
                "message": _message(str(a["code"]), a.get("message")),
                "evidence": _safe_evidence(a.get("evidence", {})),
            }
            for a in anomalies
        ],
    }
    body = CASE_TAG.sub("[case-tag]", json.dumps(case, ensure_ascii=False, indent=2))
    return f"<case>\n{body}\n</case>"


def _token(value: object) -> str | None:
    """A value kept only if it looks like an identifier: no space, so no sentence fits."""
    return value if isinstance(value, str) and SAFE_TOKEN.fullmatch(value) else None


def _message(code: str, message: object) -> str:
    """Our own description of a finding, deny by default.

    The message of a rule is sent only if the rule is known to write it from our own text and
    numbers; a rule that quotes the document gets a fixed text; any other rule, or a message
    with an unexpected shape, gets a pointer to the evidence."""
    if code in NEUTRAL_MESSAGES:
        return NEUTRAL_MESSAGES[code]
    if (
        code in PASS_THROUGH_MESSAGES
        and isinstance(message, str)
        and SAFE_MESSAGE.fullmatch(message)
    ):
        return message
    return "see the evidence"


def _safe_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_evidence(item) for item in value]
    if isinstance(value, str):
        return value if SAFE_TOKEN.fullmatch(value) else "[withheld]"
    return value


def keep_known_codes(explanation: Explanation, known: set[str]) -> Checked:
    """Drop the items about a code the model was not given; keep the first item per code."""
    kept: list[AnomalyExplanation] = []
    seen: set[str] = set()
    for item in explanation.items:
        if item.code in known and item.code not in seen:
            kept.append(item)
            seen.add(item.code)
    return Checked(
        Explanation(summary=explanation.summary, items=kept),
        dropped=len(explanation.items) - len(kept),
    )
