"""The golden dataset as evaluation cases."""

import json
from dataclasses import dataclass
from pathlib import Path

from invoice_agent.config import IngestionLimits
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.ingestion.pdf_text import extract_text

DATASET_DIR = Path(__file__).parents[1] / "tests" / "fixtures" / "invoice-test-set"

# Needs the history of earlier invoices, which a single-document evaluation does not have.
HISTORY_DEPENDENT = frozenset({"DUPLICATE_INVOICE", "PO_ALREADY_INVOICED"})
# The smoke suite: the four PDFs that need the model in production, plus an injection case.
SMOKE_CASES = ("INV-09", "INV-10", "INV-11", "INV-21", "INV-20")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    text: str
    """The text of the PDF: what the model reads."""
    expected: InvoiceData
    expected_anomalies: frozenset[str]
    """Ground-truth anomalies that can be reproduced from the document alone."""
    expected_po: str | None
    expected_supplier: str | None
    prompt_injection: bool
    plain_pdf: bool
    """True for the PDFs without an embedded XML, which need the model in production."""


def load_cases(iban_key: bytes, dataset_dir: Path = DATASET_DIR) -> list[EvalCase]:
    cases = []
    for path in sorted((dataset_dir / "ground_truth").glob("INV-*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        extraction = dict(raw["expected_extraction"])
        iban = extraction.pop("iban", None)
        if iban:
            extraction["iban"] = IbanFingerprint.from_iban(iban, iban_key)
        [pdf] = (dataset_dir / "invoices").glob(f"{raw['case_id']}_*.pdf")
        cases.append(
            EvalCase(
                case_id=raw["case_id"],
                title=raw["title"],
                text=extract_text(pdf.read_bytes(), IngestionLimits()),
                expected=InvoiceData.model_validate(extraction),
                expected_anomalies=frozenset(raw["expected_anomalies"]) - HISTORY_DEPENDENT,
                expected_po=raw["expected_po_match"],
                expected_supplier=raw["expected_supplier_code"],
                prompt_injection=bool(raw["prompt_injection"]),
                plain_pdf=raw["format"] != "facturx_en16931",
            )
        )
    return cases


def select(cases: list[EvalCase], suite: str) -> list[EvalCase]:
    if suite == "full":
        return cases
    if suite == "smoke":
        return [case for case in cases if case.case_id in SMOKE_CASES]
    if suite == "explain":
        # The cases with something to explain. One of them (INV-20) is refused before the model
        # in production, and the evaluation says so instead of hiding it.
        return [case for case in cases if case.expected_anomalies]
    raise ValueError(f"unknown suite {suite!r}: expected 'smoke', 'full' or 'explain'")
