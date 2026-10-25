"""Ingestion service: turn the bytes of a document into a stored invoice.

The extraction (parsing, possibly a slow LLM call) happens first and touches no database,
so no transaction is held open while it runs. Then, in the caller's transaction, the invoice
is stored as received, moved to extracted (or failed, with the reason) and audited.
"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy.orm import Session

from invoice_agent.config import IngestionLimits
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.invoice import Invoice, SourceDocument, SourceFormat
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.ingestion.document import ExtractionError, sha256_hex
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.pipeline import ExtractedDocument, extract_invoice
from invoice_agent.ingestion.reading import RawDocument, read_document

ACTOR_ID = "ingestion"


def ingest_document(
    session: Session,
    content: bytes,
    uri: str,
    *,
    extractor: InvoiceExtractor | None,
    iban_key: bytes,
    limits: IngestionLimits,
    run_id: UUID,
    now: datetime,
    reader: Callable[..., RawDocument] = read_document,
    trace_id: str | None = None,
) -> Invoice:
    """Store the document as an invoice: ``extracted`` on success, ``failed`` otherwise.

    A document that cannot be read still produces an invoice (``failed``, with a safe
    reason) so that the attempt is visible and auditable. Ingesting the same file twice
    creates two invoices; the second is flagged as a duplicate when it is validated.
    """
    sha256 = sha256_hex(content)
    extracted: ExtractedDocument | None = None
    failure: str | None = None
    try:
        extracted = extract_invoice(
            content, extractor=extractor, iban_key=iban_key, limits=limits, reader=reader
        )
    except ExtractionError as exc:
        failure = str(exc)

    source = SourceDocument(
        uri=uri,
        format=extracted.source_format if extracted else SourceFormat.PDF,
        sha256=sha256,
    )
    received = Invoice(id=uuid4(), source=source, received_at=now, updated_at=now)
    invoices = InvoiceRepository(session)
    events = AuditEventRepository(session)
    invoices.add(received)
    _audit(
        events,
        received,
        run_id,
        now,
        step="receive",
        payload=_with_trace({"uri": uri, "sha256": sha256, "size": len(content)}, trace_id),
    )

    if extracted is not None:
        stored = received.with_data(extracted.data, at=now).transition_to(
            InvoiceStatus.EXTRACTED, at=now
        )
        payload: dict[str, JsonValue] = {
            "outcome": "extracted",
            "format": extracted.source_format.value,
        }
    else:
        stored = received.transition_to(InvoiceStatus.FAILED, at=now, failure_reason=failure)
        payload = {"outcome": "failed", "reason": failure}
    invoices.save(stored, expected_status=InvoiceStatus.RECEIVED)
    _audit(events, stored, run_id, now, step="extract", payload=_with_trace(payload, trace_id))
    return stored


def _with_trace(payload: dict[str, JsonValue], trace_id: str | None) -> dict[str, JsonValue]:
    """Link the audit event to its Langfuse trace, so a decision can be followed in both."""
    return {**payload, "trace_id": trace_id} if trace_id else payload


def _audit(
    events: AuditEventRepository,
    invoice: Invoice,
    run_id: UUID,
    now: datetime,
    *,
    step: str,
    payload: dict[str, JsonValue],
) -> None:
    redacted = redact_payload(payload)
    assert isinstance(redacted, dict)
    events.append(
        AuditEvent(
            id=uuid4(),
            run_id=run_id,
            step=step,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            invoice_id=invoice.id,
            payload=redacted,
            occurred_at=now,
        )
    )
