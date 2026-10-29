"""What may be said about an invoice in a message to its supplier.

Like the case file of the explanation (ADR-0011), the facts hold no string from the document
except an identifier-shaped invoice number and order reference: no sentence of the document can
reach the model that polishes a draft, and none can reach the supplier through a template. The
supplier's name is not used at all: the recipient is the contact on file, and a greeting needs
no name.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from invoice_agent.agent.explanation import CASE_TAG, safe_message, safe_token
from invoice_agent.domain.invoice import InvoiceData


@dataclass(frozen=True)
class AnomalyFact:
    code: str
    message: str
    """Our own description of the finding (never a sentence of the document)."""


@dataclass(frozen=True)
class QueryFacts:
    invoice_number: str | None
    """Only when it has the shape of an identifier; otherwise the message says "your invoice"."""
    purchase_order_ref: str | None
    currency: str
    total_ttc: str
    anomalies: tuple[AnomalyFact, ...]
    reminder: bool = False
    """True for the second message of a conversation: nobody answered the first."""

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(fact.code for fact in self.anomalies)

    def case_text(self) -> str:
        """The facts as JSON between <case> tags, for a model: data to read, never instructions."""
        case: dict[str, Any] = {
            "invoice": {
                "number": self.invoice_number,
                "purchase_order_ref": self.purchase_order_ref,
                "currency": self.currency,
                "total_ttc": self.total_ttc,
            },
            "anomalies": [{"code": a.code, "message": a.message} for a in self.anomalies],
            "reminder": self.reminder,
        }
        body = CASE_TAG.sub("[case-tag]", json.dumps(case, ensure_ascii=False, indent=2))
        return f"<case>\n{body}\n</case>"


def build_facts(
    data: InvoiceData, anomalies: list[dict[str, Any]], *, reminder: bool = False
) -> QueryFacts:
    """The facts of an invoice and of the anomalies the rules found on it."""
    return QueryFacts(
        invoice_number=safe_token(data.invoice_number),
        purchase_order_ref=safe_token(data.purchase_order_ref),
        currency=data.currency,
        total_ttc=str(data.total_ttc),
        anomalies=tuple(
            AnomalyFact(code=str(a["code"]), message=safe_message(str(a["code"]), a.get("message")))
            for a in anomalies
        ),
        reminder=reminder,
    )


DIGITS = re.compile(r"\d+")


def digit_groups(value: str | None) -> set[str]:
    """The runs of digits of an identifier: ``PM-2026-0457`` gives ``2026`` and ``0457``."""
    return set(DIGITS.findall(value)) if value else set()
