"""The synthetic replies agree with the code that flags them (no model involved)."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from invoice_agent.agent.correspondence.answers import DraftReview
from invoice_agent.agent.graph.run import Graph, RunStatus, answer_pause, resume_run
from invoice_agent.agent.graph.state import ApprovalDecision, PauseKind, Verdict
from invoice_agent.db.reference_data import demo_contact_address
from invoice_agent.domain.rules.injection import find_injection_patterns
from mailroom.seed import seed_contacts
from mailroom.server import build_server
from mailroom.store import MailroomStore
from tests.integration.support import document, graph_for, start

DATASET = Path(__file__).parents[1] / "fixtures" / "supplier-replies" / "replies.json"
RAW = json.loads(DATASET.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = RAW["cases"]
ACTIONS = {"resolved", "ask_again", "give_up"}


def test_the_dataset_is_well_formed() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)) == 15
    for case in CASES:
        assert set(case["expected"]) == {
            "from_contact",
            "iban_redacted",
            "injection_suspected",
            "action",
        }
        assert case["expected"]["action"] and set(case["expected"]["action"]) <= ACTIONS
        assert case["category"] in {"settles", "incomplete", "ends", "risk"}


def test_a_risky_reply_is_never_expected_to_be_trusted_blindly() -> None:
    for case in CASES:
        flags = case["expected"]
        if flags["injection_suspected"] or not flags["from_contact"]:
            assert "resolved" not in flags["action"], case["id"]


@pytest.fixture
def store(engine: Engine, factory: sessionmaker[Session]) -> MailroomStore:
    seed_contacts(engine)
    return MailroomStore(engine)


@pytest.fixture
def thread(
    factory: sessionmaker[Session], documents: Path, saver: Any, store: MailroomStore
) -> RunStatus:
    """A run that has written to the supplier, so that replies can arrive in its thread."""
    graph: Graph = graph_for(factory, documents, saver, mailroom=build_server(store))
    run = start(graph, document(documents, "INV-12"))
    ask = ApprovalDecision(verdict=Verdict.REJECT_AND_QUERY, approver="alice")
    run = resume_run(graph, factory, UUID(run.state["invoice_id"]), ask)
    review = DraftReview(action="send", approver="alice")
    return answer_pause(graph, run.run_id, PauseKind.REVIEW_DRAFT, review)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_checks_report_what_the_dataset_expects(
    case: dict[str, Any], store: MailroomStore, thread: RunStatus
) -> None:
    contact = demo_contact_address(RAW["supplier"])
    sender = contact if case["from"] == "contact" else case["from"]
    stored = store.receive_message(thread.run_id, sender, case["subject"], case["body"])
    expected = case["expected"]
    assert stored.from_contact == expected["from_contact"]
    assert stored.iban_redacted == expected["iban_redacted"]
    suspected = bool(find_injection_patterns([stored.subject, stored.body]))
    assert suspected == expected["injection_suspected"]
    assert "FR7699053965263585803695442" not in stored.body
