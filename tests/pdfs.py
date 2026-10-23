"""Tiny PDFs built on the fly for ingestion tests."""

import io
import zlib
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


def pdf_with_many_attachments(count: int, wanted: bytes) -> bytes:
    """`count` junk attachments, all sorted before the real invoice XML (names are sorted)."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    for index in range(count):
        writer.add_attachment(f"a-{index:03d}.txt", b"x" * 10)
    writer.add_attachment("factur-x.xml", wanted)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_with_flate_bomb(filename: str, inflated_size: int) -> bytes:
    """A hand-written PDF whose attachment is tiny in the file and ``inflated_size`` inflated."""
    name = filename.encode()
    compressed = zlib.compress(b"0" * inflated_size, 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names ["
        b"(" + name + b") 4 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        b"<< /Type /Filespec /F (" + name + b") /EF << /F 5 0 R >> >>",
        b"<< /Type /EmbeddedFile /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream",
    ]
    body = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    body += f"startxref\n{xref_at}\n%%EOF".encode()
    return body


def pdf_with_content_bomb(inflated_size: int) -> bytes:
    """A one-page PDF whose content stream is tiny in the file and huge once inflated."""
    compressed = zlib.compress(b"0 " * (inflated_size // 2), 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        b"<< /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream",
    ]
    body = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    body += f"startxref\n{xref_at}\n%%EOF".encode()
    return body


def pdf_with_many_content_streams(count: int) -> bytes:
    """A one-page PDF whose /Contents is an array of ``count`` small streams."""
    streams = [b"BT /F1 12 Tf 10 10 Td (hello) Tj ET" for _ in range(count)]
    first_stream_object = 4
    refs = b" ".join(f"{first_stream_object + i} 0 R".encode() for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents [" + refs + b"] >>",
    ]
    for stream in streams:
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    body = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    body += f"startxref\n{xref_at}\n%%EOF".encode()
    return body


def pdf_with_form_invocations(count: int) -> bytes:
    """A page that draws the same form XObject ``count`` times."""
    page_content = b"/Fm0 Do\n" * count
    form_content = b"BT (x) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R"
        b" /Resources << /XObject << /Fm0 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(page_content)).encode()
        + b" >>\nstream\n"
        + page_content
        + b"endstream",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Length "
        + str(len(form_content)).encode()
        + b" >>\nstream\n"
        + form_content
        + b"\nendstream",
    ]
    body = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    body += f"startxref\n{xref_at}\n%%EOF".encode()
    return body
