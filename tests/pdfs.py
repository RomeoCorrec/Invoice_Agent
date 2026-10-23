"""Tiny PDFs built on the fly for ingestion tests."""

import io
from pathlib import Path

from pypdf import PdfWriter

INVOICES = Path(__file__).parent / "fixtures" / "invoice-test-set" / "invoices"


def blank_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_with_attachment(filename: str, data: bytes) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_attachment(filename, data)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("owner-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def golden_pdf(case_id: str) -> bytes:
    [path] = INVOICES.glob(f"{case_id}_*.pdf")
    return path.read_bytes()
