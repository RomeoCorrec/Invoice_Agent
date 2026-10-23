"""Factur-X: get the embedded CII XML out of a PDF and turn it into ``InvoiceData``.

The XML comes from an untrusted document, so it is parsed with a hardened parser (no
entities, no DTD, no network) and a DOCTYPE is refused outright. Amounts are read as
``Decimal`` from their text, never through ``float``.
"""

import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree
from pydantic import ValidationError
from pypdf import PdfReader

from invoice_agent.config import IngestionLimits
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import (
    InvoiceData,
    InvoiceLine,
    Party,
    VatBreakdownLine,
)
from invoice_agent.ingestion.document import ExtractionError

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
COMMERCIAL_INVOICE = "380"
# Names under which a CII invoice is embedded (Factur-X, ZUGFeRD 2, XRechnung CII).
XML_ATTACHMENT_NAMES = frozenset({"factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"})

_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
    remove_comments=True,
    remove_pis=True,
)


def extract_xml(content: bytes, limits: IngestionLimits) -> bytes | None:
    """The embedded Factur-X XML, or None for a plain PDF.

    The attachment is read with pypdf and handed straight to our hardened parser: no other
    library parses this untrusted XML first.
    """
    try:
        attachments = PdfReader(io.BytesIO(content)).attachments
    except Exception as exc:  # pypdf raises many types on malformed PDFs
        raise ExtractionError("could not read the PDF attachments") from exc
    for name, payloads in attachments.items():
        if name.lower() in XML_ATTACHMENT_NAMES and payloads:
            data = bytes(payloads[0])
            if len(data) > limits.max_xml_bytes:
                raise ExtractionError(f"embedded XML is larger than {limits.max_xml_bytes} bytes")
            return data
    return None


def parse_cii(xml: bytes, iban_key: bytes) -> InvoiceData:
    """Convert a CII (EN 16931) invoice to ``InvoiceData``."""
    root = _parse(xml)
    try:
        return _build(root, iban_key)
    except ExtractionError:
        raise
    except (ValidationError, ValueError, InvalidOperation) as exc:
        raise ExtractionError(f"invoice XML has an invalid value: {type(exc).__name__}") from exc


def _parse(xml: bytes) -> Any:
    try:
        root = etree.fromstring(xml, _PARSER)
    except etree.XMLSyntaxError as exc:
        raise ExtractionError("invoice XML is not well-formed") from exc
    tree = root.getroottree()
    if tree.docinfo.doctype or tree.docinfo.internalDTD is not None:
        raise ExtractionError("invoice XML contains a DOCTYPE, which is not allowed")
    if root.tag != f"{{{NS['rsm']}}}CrossIndustryInvoice":
        raise ExtractionError("invoice XML is not a CrossIndustryInvoice")
    return root


def _build(root: Any, iban_key: bytes) -> InvoiceData:
    document = _one(root, "rsm:ExchangedDocument")
    if _text(document, "ram:TypeCode") != COMMERCIAL_INVOICE:
        raise ExtractionError("only commercial invoices (type 380) are supported")
    transaction = _one(root, "rsm:SupplyChainTradeTransaction")
    agreement = _one(transaction, "ram:ApplicableHeaderTradeAgreement")
    settlement = _one(transaction, "ram:ApplicableHeaderTradeSettlement")
    totals = _one(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")

    iban = _optional_text(
        settlement,
        "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID",
    )
    buyer = agreement.find("ram:BuyerTradeParty", NS)
    return InvoiceData(
        invoice_number=_text(document, "ram:ID"),
        issue_date=_date(_one(document, "ram:IssueDateTime")),
        due_date=_optional_date(settlement, "ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime"),
        currency=_text(settlement, "ram:InvoiceCurrencyCode"),
        seller=_party(_one(agreement, "ram:SellerTradeParty")),
        buyer=_party(buyer) if buyer is not None else None,
        purchase_order_ref=_optional_text(
            agreement, "ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID"
        ),
        iban=IbanFingerprint.from_iban(iban, iban_key) if iban else None,
        notes=tuple(
            note.strip()
            for note in (
                (node.text or "") for node in document.findall("ram:IncludedNote/ram:Content", NS)
            )
            if note.strip()
        ),
        lines=tuple(
            _line(node) for node in transaction.findall("ram:IncludedSupplyChainTradeLineItem", NS)
        ),
        vat_breakdown=tuple(
            VatBreakdownLine(
                rate=_decimal(node, "ram:RateApplicablePercent"),
                basis=_decimal(node, "ram:BasisAmount"),
                vat=_decimal(node, "ram:CalculatedAmount"),
            )
            for node in settlement.findall("ram:ApplicableTradeTax", NS)
        ),
        total_ht=_decimal(totals, "ram:TaxBasisTotalAmount"),
        total_vat=_decimal(totals, "ram:TaxTotalAmount"),
        total_ttc=_decimal(totals, "ram:GrandTotalAmount"),
    )


def _party(node: Any) -> Party:
    return Party(
        name=_text(node, "ram:Name"),
        siren=_optional_text(node, "ram:SpecifiedLegalOrganization/ram:ID"),
        vat_number=_optional_text(node, "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']"),
    )


def _line(node: Any) -> InvoiceLine:
    settlement = _one(node, "ram:SpecifiedLineTradeSettlement")
    return InvoiceLine(
        description=_text(node, "ram:SpecifiedTradeProduct/ram:Name"),
        quantity=_decimal(node, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity"),
        unit_price=_decimal(
            node, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount"
        ),
        vat_rate=_decimal(settlement, "ram:ApplicableTradeTax/ram:RateApplicablePercent"),
        amount_ht=_decimal(
            settlement,
            "ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount",
        ),
    )


# --- small XML helpers ---------------------------------------------------------------------


def _one(node: Any, path: str) -> Any:
    found = node.find(path, NS)
    if found is None:
        raise ExtractionError(f"invoice XML is missing {path}")
    return found


def _optional_text(node: Any, path: str) -> str | None:
    found = node.find(path, NS)
    text = found.text.strip() if found is not None and found.text else ""
    return text or None


def _text(node: Any, path: str) -> str:
    text = _optional_text(node, path)
    if text is None:
        raise ExtractionError(f"invoice XML is missing {path}")
    return text


def _decimal(node: Any, path: str) -> Decimal:
    return Decimal(_text(node, path))


def _date(node: Any) -> date:
    stamp = _one(node, "udt:DateTimeString")
    if stamp.get("format") != "102" or not stamp.text:
        raise ExtractionError("only dates in format 102 (YYYYMMDD) are supported")
    return datetime.strptime(stamp.text.strip(), "%Y%m%d").date()


def _optional_date(node: Any, path: str) -> date | None:
    found = node.find(path, NS)
    return _date(found) if found is not None else None
