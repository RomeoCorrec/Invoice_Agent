import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invoice_agent.config import IngestionLimits
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.facturx import extract_xml, parse_cii
from tests.factories import GROUND_TRUTH, IBAN_KEY
from tests.pdfs import (
    blank_pdf,
    golden_pdf,
    pdf_with_attachment,
    pdf_with_flate_bomb,
    pdf_with_many_attachments,
)

LIMITS = IngestionLimits()
FACTURX_CASES = [
    path
    for path in sorted(GROUND_TRUTH.glob("INV-*.json"))
    if json.loads(path.read_text(encoding="utf-8"))["format"] == "facturx_en16931"
]
TEXT_ONLY_CASES = ["INV-09", "INV-10", "INV-11", "INV-21"]


def expected(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["expected_extraction"]  # type: ignore[no-any-return]


def test_the_dataset_has_17_facturx_and_4_text_only_invoices() -> None:
    assert len(FACTURX_CASES) == 17
    assert {p.stem for p in GROUND_TRUTH.glob("INV-*.json")} - {
        p.stem for p in FACTURX_CASES
    } == set(TEXT_ONLY_CASES)


@pytest.mark.parametrize("path", FACTURX_CASES, ids=lambda p: p.stem)
def test_parsed_invoice_matches_the_ground_truth(path: Path) -> None:
    xml = extract_xml(golden_pdf(path.stem), LIMITS)
    assert xml is not None
    data = parse_cii(xml, IBAN_KEY)
    truth = expected(path)

    assert data.invoice_number == truth["invoice_number"]
    assert data.issue_date.isoformat() == truth["issue_date"]
    assert (data.due_date.isoformat() if data.due_date else None) == truth["due_date"]
    assert data.currency == truth["currency"]
    assert data.seller.name == truth["seller"]["name"]
    assert data.seller.siren == truth["seller"]["siren"]
    assert data.seller.vat_number == truth["seller"]["vat_number"]
    assert data.buyer is not None
    assert (data.buyer.name, data.buyer.siren) == (truth["buyer"]["name"], truth["buyer"]["siren"])
    assert data.purchase_order_ref == truth["purchase_order_ref"]
    assert data.iban == IbanFingerprint.from_iban(truth["iban"], IBAN_KEY)
    assert [
        (
            line.description,
            str(line.quantity.normalize()),
            line.unit_price,
            line.vat_rate,
            line.amount_ht,
        )
        for line in data.lines
    ] == [
        (
            line["description"],
            str(Decimal(line["quantity"]).normalize()),
            Decimal(line["unit_price"]),
            Decimal(line["vat_rate"]),
            Decimal(line["amount_ht"]),
        )
        for line in truth["lines"]
    ]
    assert sorted((v.rate, v.basis, v.vat) for v in data.vat_breakdown) == sorted(
        (
            Decimal(v["rate"]),
            Decimal(v["basis"]),
            Decimal(v["vat"]),
        )
        for v in truth["vat_breakdown"]
    )
    assert str(data.total_ht) == truth["total_ht"]
    assert str(data.total_vat) == truth["total_vat"]
    assert str(data.total_ttc) == truth["total_ttc"]


def test_note_of_inv_18_is_kept_as_a_note() -> None:
    xml = extract_xml(golden_pdf("INV-18"), LIMITS)
    assert xml is not None
    [note] = parse_cii(xml, IBAN_KEY).notes
    assert "coordonnées bancaires" in note


def test_invoice_without_order_reference() -> None:
    xml = extract_xml(golden_pdf("INV-08"), LIMITS)
    assert xml is not None
    assert parse_cii(xml, IBAN_KEY).purchase_order_ref is None


@pytest.mark.parametrize("case_id", TEXT_ONLY_CASES)
def test_plain_pdfs_have_no_embedded_xml(case_id: str) -> None:
    assert extract_xml(golden_pdf(case_id), LIMITS) is None


def test_blank_pdf_has_no_embedded_xml() -> None:
    assert extract_xml(blank_pdf(), LIMITS) is None


def test_oversized_embedded_xml_is_refused() -> None:
    pdf = pdf_with_attachment("factur-x.xml", b"<a>" + b"x" * 5000 + b"</a>")
    with pytest.raises(ExtractionError):
        extract_xml(pdf, IngestionLimits(max_xml_bytes=1000))


def test_decompression_bomb_is_stopped_while_inflating() -> None:
    """A few KB in the file, 5 MB once inflated: refused against a 1 KB limit."""
    pdf = pdf_with_flate_bomb("factur-x.xml", inflated_size=5_000_000)
    assert len(pdf) < 20_000
    with pytest.raises(ExtractionError):
        extract_xml(pdf, IngestionLimits(max_xml_bytes=1000))


def test_only_the_expected_attachment_is_decoded() -> None:
    """A bomb under another name is never inflated."""
    pdf = pdf_with_flate_bomb("notes.bin", inflated_size=5_000_000)
    assert extract_xml(pdf, IngestionLimits(max_xml_bytes=1000)) is None


def test_the_inflate_limit_does_not_leak_outside_the_call() -> None:
    from pypdf import get_configuration

    before = get_configuration().zlib_maximum_output_length
    with pytest.raises(ExtractionError):
        extract_xml(
            pdf_with_flate_bomb("factur-x.xml", 5_000_000), IngestionLimits(max_xml_bytes=1000)
        )
    assert get_configuration().zlib_maximum_output_length == before


def test_the_number_of_attachments_examined_is_capped() -> None:
    pdf = pdf_with_many_attachments(25, b"<a/>")
    assert extract_xml(pdf, IngestionLimits()) is None  # the real one is past the cap
    assert extract_xml(pdf_with_many_attachments(3, b"<a/>"), IngestionLimits()) == b"<a/>"


# --- hostile XML -----------------------------------------------------------------------------

CII_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
ROOT_WITH_ENTITY = f'<rsm:CrossIndustryInvoice xmlns:rsm="{CII_NS}">%s</rsm:CrossIndustryInvoice>'

XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + ROOT_WITH_ENTITY % "&xxe;"
).encode()

BILLION_LAUGHS = (
    '<?xml version="1.0"?><!DOCTYPE lolz ['
    '<!ENTITY a "aaaaaaaaaa">'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
    '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
    "]>" + ROOT_WITH_ENTITY % "&d;"
).encode()


@pytest.mark.parametrize("payload", [XXE, BILLION_LAUGHS], ids=["xxe", "billion-laughs"])
def test_doctype_is_refused(payload: bytes) -> None:
    with pytest.raises(ExtractionError, match="DOCTYPE"):
        parse_cii(payload, IBAN_KEY)


def test_xxe_never_reads_local_files(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE", encoding="utf-8")
    payload = (
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "{secret.as_uri()}">]>'
        + ROOT_WITH_ENTITY % "&e;"
    ).encode()
    with pytest.raises(ExtractionError) as error:
        parse_cii(payload, IBAN_KEY)
    assert "TOP-SECRET" not in str(error.value)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not xml at all", "not well-formed"),
        (b"<a><b></a>", "not well-formed"),
        (b"<root/>", "not a CrossIndustryInvoice"),
    ],
)
def test_malformed_or_foreign_xml_is_refused(payload: bytes, message: str) -> None:
    with pytest.raises(ExtractionError, match=message):
        parse_cii(payload, IBAN_KEY)


def test_credit_notes_are_not_supported() -> None:
    xml = extract_xml(golden_pdf("INV-01"), LIMITS)
    assert xml is not None
    credit_note = xml.replace(
        b"<ram:TypeCode>380</ram:TypeCode>", b"<ram:TypeCode>381</ram:TypeCode>", 1
    )
    with pytest.raises(ExtractionError, match="type 380"):
        parse_cii(credit_note, IBAN_KEY)


def test_missing_mandatory_field_is_an_error_not_a_default() -> None:
    xml = extract_xml(golden_pdf("INV-01"), LIMITS)
    assert xml is not None
    broken = xml.replace(b"GrandTotalAmount", b"Renamed")
    with pytest.raises(ExtractionError, match="missing"):
        parse_cii(broken, IBAN_KEY)


def test_invalid_amount_is_an_error() -> None:
    xml = extract_xml(golden_pdf("INV-01"), LIMITS)
    assert xml is not None
    broken = xml.replace(b"<ram:GrandTotalAmount>360.00<", b"<ram:GrandTotalAmount>12,5x<", 1)
    with pytest.raises(ExtractionError, match="invalid value"):
        parse_cii(broken, IBAN_KEY)


def test_amounts_with_too_many_decimals_are_refused_not_rounded() -> None:
    xml = extract_xml(golden_pdf("INV-01"), LIMITS)
    assert xml is not None
    broken = xml.replace(b"<ram:GrandTotalAmount>360.00<", b"<ram:GrandTotalAmount>360.005<", 1)
    with pytest.raises(ExtractionError, match="invalid value"):
        parse_cii(broken, IBAN_KEY)


def test_inflation_is_bounded_while_decoding_not_only_checked_afterwards() -> None:
    """Peak memory stays tiny for a 30 MB bomb: the limit applies during decompression."""
    import tracemalloc

    pdf = pdf_with_flate_bomb("factur-x.xml", inflated_size=30_000_000)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError):
            extract_xml(pdf, IngestionLimits(max_xml_bytes=1000))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000
