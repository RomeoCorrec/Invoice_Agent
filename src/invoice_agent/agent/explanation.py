"""What the model is asked about an invoice with anomalies, and what is kept of its answer.

The model gets structured fields and the anomalies the rules found, never the free text of the
document (notes, descriptions). Its answer is advisory text for the reviewer: it does not
change the status, the entry or the decision, and a code it was not given is dropped.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from invoice_agent.domain.invoice import InvoiceData

CASE_TAG = re.compile(r"<\s*/?\s*case\b[^>]*>", re.IGNORECASE)
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
    """The case file the model reads: figures and findings as JSON between <case> tags."""
    case = {
        "invoice": {
            "number": data.invoice_number,
            "seller": data.seller.name,
            "seller_siren": data.seller.siren,
            "currency": data.currency,
            "total_ht": str(data.total_ht),
            "total_vat": str(data.total_vat),
            "total_ttc": str(data.total_ttc),
            "vat_breakdown": [
                {"rate": str(v.rate), "basis": str(v.basis), "vat": str(v.vat)}
                for v in data.vat_breakdown
            ],
            "purchase_order_ref": data.purchase_order_ref,
        },
        "anomalies": [
            {
                "code": a["code"],
                "message": a.get("message"),
                "evidence": a.get("evidence", {}),
            }
            for a in anomalies
        ],
    }
    # A value that contains a closing tag must not be able to end the case early.
    body = CASE_TAG.sub("[case-tag]", json.dumps(case, ensure_ascii=False, indent=2))
    return f"<case>\n{body}\n</case>"


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
