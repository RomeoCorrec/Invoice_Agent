"""What a worker does for each kind of job.

A handler runs inside the worker's transaction: whatever it writes is committed together with
the job being marked as succeeded, or rolled back together with the retry.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy.orm import Session

from invoice_agent.agent.ingestion import ingest_document
from invoice_agent.agent.validation import NotReadyForValidationError, validate_invoice
from invoice_agent.config import IngestionLimits
from invoice_agent.db.repositories import InvoiceNotFoundError, JobRepository
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.domain.job import Job, PermanentJobError
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.extractor import InvoiceExtractor
from invoice_agent.ingestion.reading import RawDocument, read_document
from invoice_agent.observability.tracing import Tracing
from invoice_agent.worker.store import DocumentStore

INGEST = "ingest"
VALIDATE = "validate"


@dataclass(frozen=True)
class JobContext:
    session: Session
    now: datetime
    store: DocumentStore
    extractor: InvoiceExtractor | None
    iban_key: bytes
    limits: IngestionLimits
    max_attempts: int
    reader: Callable[..., RawDocument] = read_document
    tracing: Tracing = field(default_factory=Tracing)


Handler = Callable[[Job, JobContext], None]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestPayload(_Payload):
    document: str


class ValidatePayload(_Payload):
    invoice_id: UUID
    trace_id: str | None = None
    """The trace opened by the ingestion job, so one invoice is one trace."""


def _payload[P: _Payload](model: type[P], job: Job) -> P:
    try:
        return model.model_validate(job.payload)
    except ValidationError as exc:
        raise PermanentJobError(f"malformed {job.kind!r} payload") from exc


def handle_ingest(job: Job, context: JobContext) -> None:
    """Read the document from the store, ingest it, and queue its validation."""
    payload = _payload(IngestPayload, job)
    trace_id = context.tracing.current_trace_id()
    try:
        content = context.store.read(payload.document, context.limits.max_document_bytes)
    except ExtractionError as exc:
        # Nothing to record as an invoice: the document is not there. Retrying cannot help.
        raise PermanentJobError(f"document unavailable: {exc}") from exc
    invoice = ingest_document(
        context.session,
        content,
        payload.document,
        extractor=context.extractor,
        iban_key=context.iban_key,
        limits=context.limits,
        run_id=job.id,
        now=context.now,
        reader=context.reader,
        trace_id=trace_id,
    )
    extracted = invoice.status is InvoiceStatus.EXTRACTED
    context.tracing.score("extraction_ok", extracted, comment=invoice.failure_reason)
    if extracted:
        payload_out: dict[str, JsonValue] = {"invoice_id": str(invoice.id)}
        if trace_id is not None:
            payload_out["trace_id"] = trace_id
        JobRepository(context.session).enqueue(
            Job(
                id=uuid4(),
                kind=VALIDATE,
                payload=payload_out,
                max_attempts=context.max_attempts,
                run_after=context.now,
                idempotency_key=f"{VALIDATE}:{invoice.id}",
                created_at=context.now,
                updated_at=context.now,
            )
        )


def handle_validate(job: Job, context: JobContext) -> None:
    payload = _payload(ValidatePayload, job)
    try:
        result = validate_invoice(
            context.session,
            payload.invoice_id,
            run_id=job.id,
            now=context.now,
            trace_id=payload.trace_id or context.tracing.current_trace_id(),
        )
    except (InvoiceNotFoundError, NotReadyForValidationError) as exc:
        raise PermanentJobError(str(exc)) from exc
    context.tracing.score("anomaly_count", len(result.anomalies))


HANDLERS: Mapping[str, Handler] = {INGEST: handle_ingest, VALIDATE: handle_validate}


def ingest_job(document: str, *, now: datetime, max_attempts: int = 5) -> Job:
    """The job that ingests one document of the store (its name is the idempotency key)."""
    return Job(
        id=uuid4(),
        kind=INGEST,
        payload={"document": document},
        max_attempts=max_attempts,
        run_after=now,
        idempotency_key=f"{INGEST}:{document}",
        created_at=now,
        updated_at=now,
    )
