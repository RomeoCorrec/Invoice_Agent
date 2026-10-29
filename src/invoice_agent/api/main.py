"""The web interface where a person approves or rejects the invoices the agent paused on.

Pages are rendered on the server (Jinja2, autoescaping on). Every route but ``/healthz``
needs an authenticated approver; a decision also needs the CSRF token of that approver and
that invoice, and is recorded in the audit log under the approver's name, not under a name the
browser sent. Text that comes from a document is escaped by the template and cleaned of
control and bidirectional characters before it is shown. The page loads no external resource
and no inline script or style, which the Content-Security-Policy enforces.
"""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from invoice_agent.agent.graph.checkpoint import open_checkpointer
from invoice_agent.agent.graph.nodes import StaleApprovalError
from invoice_agent.agent.graph.progress import Progress, run_progress
from invoice_agent.agent.graph.run import (
    CannotApproveError,
    Graph,
    NoPendingApprovalError,
    pending_approvals,
    pending_request,
    resume_run,
)
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict
from invoice_agent.api.auth import csrf_token, csrf_valid
from invoice_agent.api.config import WebConfig
from invoice_agent.api.context import decision_context
from invoice_agent.api.limits import BodyLimit
from invoice_agent.config import load_settings
from invoice_agent.db.repositories import InvoiceRepository, JobRepository
from invoice_agent.db.session import make_engine, make_session_factory, session_scope
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.observability.logging import configure_logging, log_event
from invoice_agent.observability.tracing import Tracing
from invoice_agent.presentation import safe_text
from invoice_agent.worker.handlers import PROCESS, process_job
from invoice_agent.worker.main import build_agent_graph
from invoice_agent.worker.store import LocalDocumentStore, NoDocumentStore, UploadStore

logger = logging.getLogger("invoice_agent.api")

HERE = Path(__file__).parent
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; frame-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
# The PDF is shown in a frame of our own page, so only it may be framed, and only by us.
DOCUMENT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Disposition": 'inline; filename="invoice.pdf"',
}
PDF_MAGIC = b"%PDF-"
# The upload form is not about one invoice: its CSRF token is bound to this scope instead.
UPLOAD_SCOPE = UUID(int=0)
# What the multipart envelope adds around the file.
UPLOAD_OVERHEAD = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
RESULTS = {
    "posted": "The invoice was approved and its entry was posted to the ledger.",
    "rejected": "The invoice was rejected.",
    "failed": "The invoice was approved but the ledger refused the entry: it is marked failed.",
}
ANOMALY_LABELS = {
    "VAT_AMOUNT_MISMATCH": "The VAT does not match the base and the rate",
    "TOTAL_MISMATCH": "Net plus VAT is not the gross total",
    "DUPLICATE_INVOICE": "Looks like an invoice already received",
    "UNKNOWN_SUPPLIER": "The seller is not a known supplier",
    "INVALID_SIREN": "The SIREN is invalid or is not the supplier's",
    "IBAN_MISMATCH": "The bank account is not the one on file",
    "PO_NOT_FOUND": "The purchase order does not exist for this supplier",
    "NO_PO_MATCH": "No open purchase order matches",
    "AMOUNT_EXCEEDS_PO": "The amount is above the purchase order",
    "PO_ALREADY_INVOICED": "The purchase order was already invoiced",
    "PROMPT_INJECTION_SUSPECTED": "The document contains instructions aimed at an AI",
}


def create_app(
    config: WebConfig,
    graph: Graph,
    session_factory: sessionmaker[Session],
    *,
    documents: UploadStore | None = None,
    max_document_bytes: int = 10_000_000,
    job_attempts: int = 5,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="invoice-agent approvals",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # no API documentation or schema is served
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["clean"] = safe_text
    templates.env.globals["anomaly_labels"] = ANOMALY_LABELS
    app.add_middleware(BodyLimit, path="/uploads", limit=max_document_bytes + UPLOAD_OVERHEAD)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    basic = HTTPBasic(auto_error=False)

    def render(
        request: Request, template: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    def message(request: Request, status_code: int, text: str) -> HTMLResponse:
        return render(request, "message.html", status_code, text=text, code=status_code)

    def approver(
        request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]
    ) -> str:
        if credentials is None or not config.approvers.authenticate(
            credentials.username, credentials.password
        ):
            who = safe_text(credentials.username, 40) if credentials else "-"
            log_event(logger, "login_failed", level=logging.WARNING, user=who)
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={
                    "WWW-Authenticate": 'Basic realm="invoice-agent approvals", charset="UTF-8"'
                },
            )
        return credentials.username

    Approver = Annotated[str, Depends(approver)]

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)  # a route may set a stricter variant
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 401:
            return PlainTextResponse("authentication required", 401, headers=exc.headers)
        texts = {
            403: "This form is not valid any more.",
            404: "There is nothing here.",
            413: "The file is too large.",
        }
        return message(request, exc.status_code, texts.get(exc.status_code, "The request failed."))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _exc: RequestValidationError) -> Response:
        # The input is not echoed back: it can hold anything.
        return message(request, 422, "The request is not valid.")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, user: Approver, result: str | None = None) -> HTMLResponse:
        return render(
            request,
            "queue.html",
            user=user,
            requests=pending_approvals(graph, session_factory),
            result=RESULTS.get(result or ""),
            can_upload=documents is not None,
            upload_csrf=csrf_token(config.secret, user, UPLOAD_SCOPE),
        )

    @app.get("/fragments/queue", response_class=HTMLResponse)
    def queue_fragment(request: Request, user: Approver) -> HTMLResponse:
        """The queue table alone, for the page to refresh itself."""
        return render(
            request, "_queue_table.html", requests=pending_approvals(graph, session_factory)
        )

    @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
    def invoice_page(request: Request, invoice_id: UUID, user: Approver) -> HTMLResponse:
        waiting = pending_request(graph, session_factory, invoice_id)
        if waiting is None:
            return message(
                request, 404, "No run is waiting for a decision on this invoice (already decided?)."
            )
        with session_scope(session_factory) as session:
            context = decision_context(session, invoice_id)
        return render(
            request,
            "invoice.html",
            user=user,
            invoice=waiting,
            invoice_id=invoice_id,
            has_document=documents is not None,
            context=context,
            csrf=csrf_token(config.secret, user, invoice_id),
        )

    @app.get("/invoices/{invoice_id}/document")
    def document(request: Request, invoice_id: UUID, user: Approver) -> Response:
        """The stored PDF of an invoice. The name comes from our database, never from the URL."""
        if documents is None:
            return message(request, 404, "Documents are not available on this server.")
        with session_scope(session_factory) as session:
            invoice = InvoiceRepository(session).get(invoice_id)
            name = invoice.source.uri if invoice else None
        if name is None:
            return message(request, 404, "There is nothing here.")
        try:
            content = documents.read(name, max_document_bytes)
        except ExtractionError:
            return message(request, 404, "The document is not available.")
        if not content.startswith(PDF_MAGIC):
            return message(request, 404, "The document is not a PDF.")
        return Response(content, media_type="application/pdf", headers=DOCUMENT_HEADERS)

    @app.post("/uploads")
    def upload(
        request: Request,
        user: Approver,
        file: Annotated[UploadFile, File()],
        csrf: Annotated[str, Form()],
    ) -> Response:
        """Store the PDF under a generated name and queue it for a worker."""
        if documents is None:
            return message(request, 404, "Uploads are not available on this server.")
        if not csrf_valid(config.secret, user, UPLOAD_SCOPE, csrf):
            log_event(logger, "csrf_refused", level=logging.WARNING, user=safe_text(user, 40))
            return message(request, 403, "This form is not valid any more. Reload the page.")
        content = file.file.read(max_document_bytes + 1)
        if len(content) > max_document_bytes:
            return message(request, 413, "The file is too large.")
        if not content.startswith(PDF_MAGIC):
            return message(request, 422, "The file is not a PDF.")
        name = documents.save(content)  # the name the client gave the file is never used
        with session_scope(session_factory) as session:
            job = JobRepository(session).enqueue(
                process_job(name, now=datetime.now(UTC), max_attempts=job_attempts)
            )
        log_event(logger, "upload", user=safe_text(user, 40), job=str(job.id), bytes=len(content))
        return RedirectResponse(f"/uploads/{job.id}", 303)

    def progress_of_job(job_id: UUID) -> Progress | None:
        with session_scope(session_factory) as session:
            return run_progress(session, job_id, PROCESS, datetime.now(UTC))

    @app.get("/uploads/{job_id}", response_class=HTMLResponse)
    def upload_page(request: Request, job_id: UUID, user: Approver) -> HTMLResponse:
        progress = progress_of_job(job_id)
        if progress is None:
            return message(request, 404, "There is nothing here.")
        return render(request, "upload.html", user=user, job_id=job_id, progress=progress)

    @app.get("/uploads/{job_id}/progress", response_class=HTMLResponse)
    def upload_progress(request: Request, job_id: UUID, user: Approver) -> HTMLResponse:
        progress = progress_of_job(job_id)
        if progress is None:
            return message(request, 404, "There is nothing here.")
        return render(request, "_progress.html", job_id=job_id, progress=progress)

    @app.post("/invoices/{invoice_id}/decision")
    def decide(
        request: Request,
        invoice_id: UUID,
        user: Approver,
        verdict: Annotated[Literal["approve", "reject"], Form()],
        csrf: Annotated[str, Form()],
        comment: Annotated[str, Form(max_length=1000)] = "",
    ) -> Response:
        if not csrf_valid(config.secret, user, invoice_id, csrf):
            log_event(logger, "csrf_refused", level=logging.WARNING, user=safe_text(user, 40))
            return message(request, 403, "This form is not valid any more. Reload the page.")
        try:
            decision = ApprovalDecision(
                verdict=Verdict(verdict), approver=user, comment=comment.strip() or None
            )
            outcome = resume_run(graph, session_factory, invoice_id, decision)
        except ValidationError:
            return message(request, 422, "The decision is not valid.")
        except NoPendingApprovalError:
            return message(request, 409, "This invoice was already decided by someone else.")
        except StaleApprovalError:
            return message(request, 409, "The invoice changed since the page was shown.")
        except CannotApproveError as exc:
            return message(request, 422, str(exc))
        status = str(outcome.state.get("status", ""))
        log_event(logger, "decision", user=safe_text(user, 40), verdict=verdict, status=status)
        return RedirectResponse(f"/?result={status if status in RESULTS else 'rejected'}", 303)

    return app


def app_from_env() -> FastAPI:
    """For ``uvicorn invoice_agent.api.main:app_from_env --factory``."""
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = load_settings()
    web = WebConfig.from_env(os.environ)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    stack = ExitStack()
    saver = stack.enter_context(open_checkpointer(settings.database_url))
    graph = build_agent_graph(
        settings, factory, Tracing.from_env(os.environ), None, saver, NoDocumentStore()
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        stack.close()
        engine.dispose()

    root = settings.worker.document_root
    return create_app(
        web,
        graph,
        factory,
        documents=LocalDocumentStore(root) if root else None,
        max_document_bytes=settings.limits.max_document_bytes,
        job_attempts=settings.worker.max_attempts,
        lifespan=lifespan,
    )
