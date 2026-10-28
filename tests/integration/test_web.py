"""The approval web interface, driven like a browser would, on a real Postgres."""

import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.explanation import AnomalyExplanation, Explanation
from invoice_agent.agent.graph.deps import ExplainConfig
from invoice_agent.agent.graph.run import Graph, start_run
from invoice_agent.api import main as web
from invoice_agent.api.auth import Approvers, csrf_token, hash_password
from invoice_agent.api.config import WebConfig
from invoice_agent.db.repositories import AuditEventRepository, JobRepository
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import ActorType
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.worker.store import LocalDocumentStore
from ledger import tables
from tests.fakes import FakeToolProvider
from tests.integration.support import document, graph_for, start, status_of
from tests.pdfs import INVOICES

SECRET = b"web-secret-" + b"x" * 24
ALICE = ("alice", "alice-password")
BOB = ("bob", "bob-password")
HASHES = {name: hash_password(password) for name, password in (ALICE, BOB)}
CSRF_FIELD = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture(scope="module")
def config() -> WebConfig:
    approvers = Approvers.parse(",".join(f"{name}={stored}" for name, stored in HASHES.items()))
    return WebConfig(approvers=approvers, secret=SECRET)


def explaining(user: str) -> Explanation:
    codes = re.findall(r'"code": "([A-Z_]+)"', user)
    return Explanation(
        summary="Facture à examiner.",
        items=[AnomalyExplanation(code=c, explanation="Vérifier le taux de TVA.") for c in codes],
    )


@pytest.fixture
def explainer() -> ExplainConfig:
    return ExplainConfig(
        provider=FakeToolProvider(explaining),  # type: ignore[arg-type]
        prompt=load_prompt("explain_anomalies", 1),
    )


@pytest.fixture
def waiting(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    ledger_server: MCPServer,
    explainer: ExplainConfig,
) -> tuple[Graph, dict[str, UUID]]:
    """INV-01 (clean) and INV-12 (one anomaly, explained), both paused for approval."""
    graph = graph_for(factory, documents, saver, ledger=ledger_server, explain=explainer)
    ids = {
        case: UUID(start(graph, document(documents, case)).state["invoice_id"])
        for case in ("INV-01", "INV-12")
    }
    return graph, ids


@pytest.fixture
def client(
    config: WebConfig,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    documents: Path,
) -> Iterator[TestClient]:
    graph, _ = waiting
    app = web.create_app(config, graph, factory, documents=LocalDocumentStore(documents))
    with TestClient(app, follow_redirects=False) as c:
        yield c


def ids(waiting: tuple[Graph, dict[str, UUID]]) -> dict[str, UUID]:
    return waiting[1]


def token_of(client: TestClient, invoice_id: UUID, user: tuple[str, str] = ALICE) -> str:
    page = client.get(f"/invoices/{invoice_id}", auth=user)
    match = CSRF_FIELD.search(page.text)
    assert match, page.text
    return match.group(1)


def decide(
    client: TestClient,
    invoice_id: UUID,
    verdict: str = "approve",
    *,
    user: tuple[str, str] = ALICE,
    csrf: str | None = None,
    **fields: str,
) -> Any:
    token = csrf if csrf is not None else token_of(client, invoice_id, user)
    return client.post(
        f"/invoices/{invoice_id}/decision",
        data={"verdict": verdict, "csrf": token, **fields},
        auth=user,
    )


# --- who may come in -------------------------------------------------------------------------


def test_the_health_check_needs_no_login_and_shows_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("auth", [None, ("alice", "wrong"), ("nobody", "alice-password"), ("", "")])
def test_every_page_needs_a_valid_login(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], auth: tuple[str, str] | None
) -> None:
    invoice = ids(waiting)["INV-01"]
    for response in (
        client.get("/", auth=auth),
        client.get(f"/invoices/{invoice}", auth=auth),
        client.post(
            f"/invoices/{invoice}/decision", data={"verdict": "approve", "csrf": "x"}, auth=auth
        ),
    ):
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")
        assert "INV" not in response.text and "PM-2026" not in response.text


def test_no_api_documentation_is_served(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, auth=ALICE).status_code == 404


def test_a_failed_login_is_logged_without_the_password(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="invoice_agent.api"):
        client.get("/", auth=("alice", "SuperSecretGuess"))
    logged = " ".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "login_failed" in logged and "alice" in logged
    assert "SuperSecretGuess" not in logged


def test_every_page_carries_the_security_headers(client: TestClient) -> None:
    for response in (
        client.get("/healthz"),
        client.get("/", auth=ALICE),
        client.get("/", auth=None),
    ):
        headers = response.headers
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert (
            headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
        )


def test_pages_load_nothing_external_and_run_no_inline_code(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    for path in ("/", f"/invoices/{ids(waiting)['INV-12']}"):
        body = client.get(path, auth=ALICE).text
        # the only script is the vendored htmx, from our own origin, with no code in the page
        assert re.findall(r"<script[^>]*>", body) == ['<script src="/static/htmx.min.js" defer>']
        assert "defer></script>" in body and " style=" not in body  # the tag is empty
        assert "<style" not in body and not re.search(r" on\w+=", body)
        assert not re.search(r'(?:src|href)="https?://', body)


# --- what the approver sees ------------------------------------------------------------------


def test_the_queue_lists_what_is_waiting(client: TestClient) -> None:
    page = client.get("/", auth=ALICE).text
    assert "Signed in as alice" in page
    assert "PM-2026-0457" in page and "CDA-26-0466" in page
    assert "VAT_AMOUNT_MISMATCH" in page


def test_the_page_of_an_invoice_shows_the_figures_the_explanation_and_the_entry(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    page = client.get(f"/invoices/{ids(waiting)['INV-12']}", auth=ALICE).text
    assert "976.80" in page and "VAT_AMOUNT_MISMATCH" in page
    assert "The VAT does not match the base and the rate" in page
    assert "Written by the model" in page and "Vérifier le taux de TVA." in page
    assert "6257" in page and "44566" in page  # the entry that would be posted
    assert "Approve and post" in page and "Reject" in page


def test_a_clean_invoice_has_no_explanation_section(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    page = client.get(f"/invoices/{ids(waiting)['INV-01']}", auth=ALICE).text
    assert "The rules found nothing." in page and "Written by the model" not in page


def test_text_from_a_document_cannot_inject_markup_or_fake_a_line(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice = ids(waiting)["INV-12"]
    rlo = chr(0x202E)  # right-to-left override: makes a number read backwards
    hostile = f"<script>alert(1)</script><img src=x onerror=alert(2)>\r\n{rlo}evil"
    line = {"account": hostile, "side": "debit", "amount": "1.00", "label": hostile}
    request = {
        "invoice_id": str(invoice),
        "invoice_number": hostile,
        "seller": hostile,
        "currency": "EUR",
        "total_ht": "1.00",
        "total_vat": "0.20",
        "total_ttc": hostile,
        "status": "needs_review",
        "anomaly_codes": [hostile],
        "explanation": {"summary": hostile, "items": [{"code": hostile, "explanation": hostile}]},
        "entry": {"journal": "ACH", "entry_date": "2026-10-01", "lines": [line]},
        "entry_blocked": None,
    }
    monkeypatch.setattr(web, "pending_request", lambda *_a, **_k: request)
    monkeypatch.setattr(web, "pending_approvals", lambda *_a, **_k: [request])
    for path in ("/", f"/invoices/{invoice}"):
        body = client.get(path, auth=ALICE).text
        assert "<script>" not in body and "<img" not in body
        assert "&lt;script&gt;" in body  # shown as text, not run
        assert "\r" not in body and rlo not in body


# --- deciding --------------------------------------------------------------------------------


def test_approving_posts_the_entry_and_records_the_logged_in_approver(
    client: TestClient,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    invoice = ids(waiting)["INV-01"]
    response = decide(client, invoice, "approve", comment="looks right")
    assert response.status_code == 303 and response.headers["location"] == "/?result=posted"
    assert status_of(factory, str(invoice)) is InvoiceStatus.POSTED
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(tables.entries)).scalar_one() == 1
        )
    with session_scope(factory) as session:
        [approval] = [
            e
            for e in AuditEventRepository(session).list_for_invoice(invoice)
            if e.step == "approval"
        ]
    assert approval.actor_type is ActorType.HUMAN and approval.actor_id == "alice"
    assert approval.payload["comment"] == "looks right"
    page = client.get("/?result=posted", auth=ALICE).text
    assert "its entry was posted" in page and "PM-2026-0457" not in page  # no longer waiting


def test_a_name_sent_with_the_form_cannot_replace_the_logged_in_approver(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-01"]
    decide(client, invoice, "approve", approver="mallory")
    with session_scope(factory) as session:
        events = AuditEventRepository(session).list_for_invoice(invoice)
    assert [e.actor_id for e in events if e.step == "approval"] == ["alice"]


def test_rejecting_ends_the_run_without_posting(
    client: TestClient,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    invoice = ids(waiting)["INV-12"]
    response = decide(client, invoice, "reject", user=BOB, comment="wrong VAT rate")
    assert response.headers["location"] == "/?result=rejected"
    assert status_of(factory, str(invoice)) is InvoiceStatus.REJECTED
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(tables.entries)).scalar_one() == 0
        )


# --- forms that are not the approver's ------------------------------------------------------


def test_a_decision_without_the_csrf_field_is_refused_and_changes_nothing(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-01"]
    response = client.post(f"/invoices/{invoice}/decision", data={"verdict": "approve"}, auth=ALICE)
    assert response.status_code == 422
    assert status_of(factory, str(invoice)) is InvoiceStatus.VALIDATED


@pytest.mark.parametrize("who", ["wrong", "other_invoice", "other_approver"])
def test_a_csrf_token_that_is_not_for_this_approver_and_invoice_is_refused(
    client: TestClient,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    who: str,
) -> None:
    invoice, other = ids(waiting)["INV-01"], ids(waiting)["INV-12"]
    tokens = {
        "wrong": "0" * 64,
        "other_invoice": csrf_token(SECRET, "alice", other),
        "other_approver": csrf_token(SECRET, "bob", invoice),
    }
    response = decide(client, invoice, "approve", csrf=tokens[who])
    assert response.status_code == 403
    assert status_of(factory, str(invoice)) is InvoiceStatus.VALIDATED


# --- decisions that cannot be taken ----------------------------------------------------------


def test_a_second_decision_is_told_it_came_too_late_and_changes_nothing(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-01"]
    token_a, token_b = token_of(client, invoice), token_of(client, invoice, BOB)
    assert decide(client, invoice, "approve", csrf=token_a).status_code == 303
    late = decide(client, invoice, "reject", user=BOB, csrf=token_b)
    assert late.status_code == 409 and "already decided" in late.text
    assert status_of(factory, str(invoice)) is InvoiceStatus.POSTED
    with session_scope(factory) as session:
        steps = [e.step for e in AuditEventRepository(session).list_for_invoice(invoice)]
    assert steps.count("approval") == 1


def test_an_invoice_with_no_entry_cannot_be_approved_from_the_page(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    ledger_server: MCPServer,
    config: WebConfig,
) -> None:
    with session_scope(factory) as session:
        session.execute(text("TRUNCATE suppliers, purchase_orders CASCADE"))
    graph = graph_for(factory, documents, saver, ledger=ledger_server)
    invoice = UUID(start(graph, document(documents, "INV-09")).state["invoice_id"])
    with TestClient(web.create_app(config, graph, factory), follow_redirects=False) as client:
        page = client.get(f"/invoices/{invoice}", auth=ALICE).text
        assert "No entry can be proposed" in page and "unknown_supplier" in page
        assert "Approve and post" not in page and "Reject" in page
        refused = decide(client, invoice, "approve")
        assert refused.status_code == 422 and "can only be rejected" in refused.text
        assert decide(client, invoice, "reject").status_code == 303
    assert status_of(factory, str(invoice)) is InvoiceStatus.REJECTED


def test_unknown_or_malformed_invoices_get_a_clean_page(client: TestClient) -> None:
    missing = client.get(f"/invoices/{uuid4()}", auth=ALICE)
    assert missing.status_code == 404 and "already decided" in missing.text
    malformed = client.get("/invoices/not-a-uuid", auth=ALICE)
    assert malformed.status_code == 422 and "not-a-uuid" not in malformed.text
    nowhere = client.get("/invoices/<script>alert(1)</script>", auth=ALICE)  # no such route
    assert nowhere.status_code == 404 and "<script>" not in nowhere.text


def test_a_too_long_comment_is_refused(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-01"]
    response = decide(client, invoice, "approve", comment="x" * 1001)
    assert response.status_code == 422
    assert status_of(factory, str(invoice)) is InvoiceStatus.VALIDATED


def test_a_verdict_other_than_approve_or_reject_is_refused(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-01"]
    assert decide(client, invoice, "maybe").status_code == 422
    assert status_of(factory, str(invoice)) is InvoiceStatus.VALIDATED


# --- the document next to the invoice ----------------------------------------------------------


def test_the_page_of_an_invoice_shows_its_document_in_a_frame(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    invoice = ids(waiting)["INV-12"]
    page = client.get(f"/invoices/{invoice}", auth=ALICE).text
    assert f'<iframe src="/invoices/{invoice}/document"' in page
    assert (
        "frame-src 'self'"
        in client.get(f"/invoices/{invoice}", auth=ALICE).headers["content-security-policy"]
    )


def test_the_document_is_the_stored_pdf_served_with_strict_headers(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], documents: Path
) -> None:
    response = client.get(f"/invoices/{ids(waiting)['INV-12']}/document", auth=ALICE)
    assert response.status_code == 200
    assert response.content == (documents / document(documents, "INV-12")).read_bytes()
    headers = response.headers
    assert headers["content-type"] == "application/pdf"
    assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"
    assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["content-disposition"].startswith("inline")


def test_the_document_needs_a_login(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    assert client.get(f"/invoices/{ids(waiting)['INV-12']}/document").status_code == 401


@pytest.mark.parametrize("path", ["not-a-uuid", str(uuid4()), "../../etc/passwd"])
def test_an_unknown_or_malformed_invoice_has_no_document(client: TestClient, path: str) -> None:
    response = client.get(f"/invoices/{path}/document", auth=ALICE)
    assert response.status_code in (404, 422) and b"%PDF" not in response.content


def test_the_document_name_comes_from_the_database_not_from_the_request(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    invoice = ids(waiting)["INV-12"]
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE invoices SET source_uri = :uri WHERE id = :id"),
            {"uri": "../../outside.pdf", "id": invoice},
        )
    response = client.get(f"/invoices/{invoice}/document", auth=ALICE)
    assert response.status_code == 404 and b"%PDF" not in response.content


def test_a_stored_file_that_is_not_a_pdf_is_not_served(
    config: WebConfig,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    graph, found = waiting
    (tmp_path / "x.pdf").write_bytes(b"<html><script>alert(1)</script></html>")
    invoice = found["INV-12"]
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE invoices SET source_uri = 'x.pdf' WHERE id = :id"), {"id": invoice}
        )
    app = web.create_app(config, graph, factory, documents=LocalDocumentStore(tmp_path))
    with TestClient(app) as c:
        response = c.get(f"/invoices/{invoice}/document", auth=ALICE)
    assert response.status_code == 404 and b"alert(1)" not in response.content


def test_without_a_document_store_the_page_says_so_and_no_document_is_served(
    config: WebConfig, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    graph, found = waiting
    with TestClient(web.create_app(config, graph, factory)) as c:
        page = c.get(f"/invoices/{found['INV-12']}", auth=ALICE).text
        assert "<iframe" not in page and "DOCUMENT_ROOT" in page
        assert c.get(f"/invoices/{found['INV-12']}/document", auth=ALICE).status_code == 404


# --- the queue refreshes itself ----------------------------------------------------------------


def test_the_queue_page_asks_for_its_own_refresh_and_works_without_javascript(
    client: TestClient,
) -> None:
    page = client.get("/", auth=ALICE).text
    assert 'hx-get="/fragments/queue"' in page and 'hx-trigger="every 5s"' in page
    assert '<meta http-equiv="refresh"' in page  # inside <noscript>
    assert "PM-2026-0457" in page  # the table is in the page itself, not loaded afterwards


def test_the_fragment_is_the_table_alone_and_needs_a_login(client: TestClient) -> None:
    assert client.get("/fragments/queue").status_code == 401
    fragment = client.get("/fragments/queue", auth=ALICE).text
    assert "PM-2026-0457" in fragment and "<html" not in fragment and "Signed in" not in fragment


def test_a_decided_invoice_leaves_the_refreshed_queue(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]]
) -> None:
    invoice = ids(waiting)["INV-01"]
    assert "PM-2026-0457" in client.get("/fragments/queue", auth=ALICE).text
    assert decide(client, invoice, "reject").status_code == 303
    assert "PM-2026-0457" not in client.get("/fragments/queue", auth=ALICE).text


# --- submitting an invoice, and following it --------------------------------------------------


def upload_token(user: str = "alice") -> str:
    return csrf_token(SECRET, user, web.UPLOAD_SCOPE)


def upload(
    client: TestClient,
    content: bytes,
    *,
    filename: str = "invoice.pdf",
    csrf: str | None = None,
    user: tuple[str, str] = ALICE,
) -> Any:
    return client.post(
        "/uploads",
        data={"csrf": csrf if csrf is not None else upload_token(user[0])},
        files={"file": (filename, content, "application/pdf")},
        auth=user,
    )


def sample_pdf(case: str) -> bytes:
    """A Factur-X sample that the `waiting` fixture has not ingested yet."""
    [path] = INVOICES.glob(f"{case}_*.pdf")
    return path.read_bytes()


def jobs_in(factory: sessionmaker[Session]) -> int:
    with session_scope(factory) as session:
        return int(session.execute(text("SELECT count(*) FROM jobs")).scalar_one())


STAGE = re.compile(r'<li class="stage (\w+)">\s*<span[^>]*></span>\s*<span class="label">([^<]+)')


def stage_states(page: str) -> dict[str, str]:
    return {label: state for state, label in STAGE.findall(page)}


def claim_and_run(
    graph: Graph, factory: sessionmaker[Session], location: str
) -> tuple[UUID, dict[str, Any]]:
    """What a worker does with the queued job: claim it, run the graph, mark it succeeded."""
    job_id = UUID(location.rsplit("/", 1)[1])
    with session_scope(factory) as session:
        claimed = JobRepository(session).claim(
            "w-1", now=datetime.now(UTC), lease=timedelta(minutes=5)
        )
    assert claimed is not None and claimed.id == job_id
    run = start_run(graph, run_id=job_id, document=str(claimed.payload["document"]))
    with session_scope(factory) as session:
        JobRepository(session).succeed(job_id, "w-1", now=datetime.now(UTC))
    return job_id, dict(run.state)


def test_the_queue_offers_an_upload_form_with_its_own_token(client: TestClient) -> None:
    page = client.get("/", auth=ALICE).text
    assert 'action="/uploads"' in page and 'enctype="multipart/form-data"' in page
    assert f'value="{upload_token()}"' in page and 'accept="application/pdf"' in page


def test_an_uploaded_pdf_is_stored_under_a_generated_name_and_queued(
    client: TestClient, factory: sessionmaker[Session], documents: Path
) -> None:
    content = sample_pdf("INV-05")
    response = upload(client, content, filename="../../evil name.pdf")
    assert response.status_code == 303
    match = re.fullmatch(r"/uploads/([0-9a-f-]{36})", response.headers["location"])
    assert match
    with session_scope(factory) as session:
        job = JobRepository(session).get(UUID(match.group(1)))
    assert job is not None and job.kind == "process" and job.status.value == "queued"
    name = str(job.payload["document"])
    assert re.fullmatch(r"uploads/[0-9a-f-]{36}\.pdf", name)  # nothing from the client's name
    assert (documents / name).read_bytes() == content


def test_the_same_file_twice_makes_two_documents_not_an_overwrite(
    client: TestClient, documents: Path
) -> None:
    content = sample_pdf("INV-05")
    first = upload(client, content).headers["location"]
    second = upload(client, content).headers["location"]
    assert first != second
    assert len(list((documents / "uploads").glob("*.pdf"))) == 2


@pytest.mark.parametrize(
    "content", [b"<html><script>alert(1)</script></html>", b"", b"%PD", b"x" * 100]
)
def test_a_file_that_is_not_a_pdf_is_refused_and_nothing_is_kept(
    client: TestClient, factory: sessionmaker[Session], documents: Path, content: bytes
) -> None:
    before = jobs_in(factory)
    response = upload(client, content, filename="invoice.pdf")  # the name claims it is
    assert response.status_code == 422 and "not a PDF" in response.text
    assert jobs_in(factory) == before and not (documents / "uploads").exists()


def multipart(csrf: str, content: bytes, boundary: str) -> bytes:
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    )
    return head.encode() + content + f"\r\n--{boundary}--\r\n".encode()


def test_a_file_over_the_limit_is_refused_before_it_is_kept(
    config: WebConfig,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    documents: Path,
) -> None:
    graph, _ = waiting
    app = web.create_app(
        config, graph, factory, documents=LocalDocumentStore(documents), max_document_bytes=2000
    )
    big = b"%PDF-" + b"x" * 200_000
    before = jobs_in(factory)
    body = multipart(upload_token(), big, "b0undary")

    def chunks() -> Iterator[bytes]:  # no Content-Length: counted as it arrives
        for i in range(0, len(body), 8192):
            yield body[i : i + 8192]

    with TestClient(app) as c:
        assert upload(c, big).status_code == 413  # declared in Content-Length
        chunked = c.post(
            "/uploads",
            content=chunks(),
            headers={"content-type": "multipart/form-data; boundary=b0undary"},
            auth=ALICE,
        )
        assert chunked.status_code == 413
    assert jobs_in(factory) == before and not (documents / "uploads").exists()


def test_an_upload_needs_a_login_and_the_right_token(
    client: TestClient, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    before = jobs_in(factory)
    assert upload(client, b"%PDF-1.4", user=("alice", "wrong")).status_code == 401
    decision_token = token_of(
        client, ids(waiting)["INV-12"]
    )  # a decision token is not an upload one
    for token in ("0" * 64, decision_token, upload_token("bob")):
        assert upload(client, b"%PDF-1.4", csrf=token).status_code == 403
    missing = client.post(
        "/uploads", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}, auth=ALICE
    )
    assert missing.status_code == 422
    assert jobs_in(factory) == before


def test_the_progress_pages_need_a_login_and_a_known_job(client: TestClient) -> None:
    for path in (f"/uploads/{uuid4()}", f"/uploads/{uuid4()}/progress"):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=ALICE).status_code == 404
    assert client.get("/uploads/not-a-uuid", auth=ALICE).status_code == 422


def test_a_queued_upload_shows_the_queue_stage_and_asks_to_be_refreshed(
    client: TestClient, documents: Path
) -> None:
    location = upload(client, sample_pdf("INV-05")).headers["location"]
    page = client.get(location, auth=ALICE).text
    states = stage_states(page)
    assert states["In the queue"] == "active" and states["Reading the document"] == "pending"
    assert f'hx-get="{location}/progress"' in page and 'hx-trigger="every 1s"' in page
    assert '<meta http-equiv="refresh" content="3">' in page  # without JavaScript


def test_the_stages_follow_the_run_until_it_is_ready_for_a_decision(
    client: TestClient,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    documents: Path,
) -> None:
    graph, _ = waiting
    location = upload(client, sample_pdf("INV-05")).headers["location"]
    with session_scope(factory) as session:  # a worker takes the job: it is out of the queue
        assert JobRepository(session).claim(
            "w-1", now=datetime.now(UTC), lease=timedelta(minutes=5)
        )
    running = client.get(f"{location}/progress", auth=ALICE).text
    assert stage_states(running)["In the queue"] == "done"
    assert stage_states(running)["Reading the document"] == "active" and "hx-get" in running

    with session_scope(factory) as session:  # hand the claimed job back to the helper
        session.execute(text("UPDATE jobs SET status='queued', locked_by=NULL, locked_until=NULL"))
    claim_and_run(graph, factory, location)
    done = client.get(f"{location}/progress", auth=ALICE).text
    states = stage_states(done)
    assert states["Ready for your decision"] == "done"
    assert all(state == "done" for state in states.values())
    assert "hx-get" not in done  # the polling stops by itself
    link = re.search(r'href="(/invoices/[0-9a-f-]{36})"', done)
    assert link and "Review the invoice and decide" in done
    assert client.get(link.group(1), auth=ALICE).status_code == 200


def test_a_decided_invoice_says_so_instead_of_offering_the_decision(
    client: TestClient,
    waiting: tuple[Graph, dict[str, UUID]],
    factory: sessionmaker[Session],
    documents: Path,
) -> None:
    graph, _ = waiting
    location = upload(client, sample_pdf("INV-05")).headers["location"]
    _, state = claim_and_run(graph, factory, location)
    assert decide(client, UUID(state["invoice_id"]), "reject").status_code == 303
    page = client.get(f"{location}/progress", auth=ALICE).text
    assert "already decided" in page and "Review the invoice" not in page


def test_a_failed_run_marks_the_stage_and_shows_a_cleaned_reason(
    client: TestClient, factory: sessionmaker[Session], documents: Path
) -> None:
    location = upload(client, sample_pdf("INV-05")).headers["location"]
    job_id = UUID(location.rsplit("/", 1)[1])
    with session_scope(factory) as session:
        jobs = JobRepository(session)
        assert jobs.claim("w-1", now=datetime.now(UTC), lease=timedelta(minutes=5))
        jobs.die(job_id, "w-1", now=datetime.now(UTC), error="unreadable <script>alert(1)</script>")
    page = client.get(f"{location}/progress", auth=ALICE).text
    assert stage_states(page)["Reading the document"] == "failed"
    assert "The processing failed" in page and "<script>" not in page and "hx-get" not in page


def test_a_job_nobody_picks_up_says_that_a_worker_may_be_missing(
    client: TestClient, factory: sessionmaker[Session], documents: Path
) -> None:
    location = upload(client, sample_pdf("INV-05")).headers["location"]
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE jobs SET created_at = :at"),
            {"at": datetime.now(UTC) - timedelta(minutes=2)},
        )
    page = client.get(f"{location}/progress", auth=ALICE).text
    assert "No worker has picked this up yet" in page


def test_without_a_document_store_there_is_no_upload(
    config: WebConfig, waiting: tuple[Graph, dict[str, UUID]], factory: sessionmaker[Session]
) -> None:
    graph, _ = waiting
    with TestClient(web.create_app(config, graph, factory)) as c:
        assert "/uploads" not in c.get("/", auth=ALICE).text
        assert upload(c, b"%PDF-1.4").status_code == 404
