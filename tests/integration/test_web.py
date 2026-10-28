"""The approval web interface, driven like a browser would, on a real Postgres."""

import logging
import re
from collections.abc import Iterator
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
from invoice_agent.agent.graph.run import Graph
from invoice_agent.api import main as web
from invoice_agent.api.auth import Approvers, csrf_token, hash_password
from invoice_agent.api.config import WebConfig
from invoice_agent.db.repositories import AuditEventRepository
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import ActorType
from invoice_agent.domain.invoice_status import InvoiceStatus
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.worker.store import LocalDocumentStore
from ledger import tables
from tests.fakes import FakeToolProvider
from tests.integration.support import document, graph_for, start, status_of

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
