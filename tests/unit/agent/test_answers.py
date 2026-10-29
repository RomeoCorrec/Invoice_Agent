from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.agent.correspondence.answers import DraftReview, ReplyReview, WaitEnded
from invoice_agent.agent.graph.state import ApprovalDecision, Verdict

IBAN = "FR7699053965263585803695442"


def test_a_draft_can_be_sent_as_it_is_or_with_both_parts_edited() -> None:
    assert DraftReview(action="send", approver="alice").subject is None
    edited = DraftReview(action="send", approver="alice", subject="Un objet", body="Un texte")
    assert (edited.subject, edited.body) == ("Un objet", "Un texte")


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "send", "subject": "only a subject"},
        {"action": "send", "body": "only a body"},
        {"action": "discard", "subject": "s", "body": "b"},  # nothing to edit in what is dropped
        {"action": "send", "subject": "two\nlines", "body": "b"},  # a header cannot be smuggled in
        {"action": "send", "subject": "s", "body": ""},
        {"action": "send", "subject": "s", "body": "x" * 5001},
        {"action": "forward", "approver": "alice"},
        {"action": "send", "approver": ""},
    ],
)
def test_a_draft_review_that_makes_no_sense_is_refused(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        DraftReview(**{"approver": "alice", **fields})  # type: ignore[arg-type]


def test_a_reply_names_its_message_and_a_timeout_does_not() -> None:
    message = uuid4()
    assert WaitEnded(event="reply", message_id=message).message_id == message
    assert WaitEnded(event="timeout").message_id is None
    for fields in ({"event": "reply"}, {"event": "timeout", "message_id": message}):
        with pytest.raises(ValidationError):
            WaitEnded(**fields)  # type: ignore[arg-type]


def test_a_note_never_keeps_bank_details() -> None:
    review = ReplyReview(action="resolved", approver="alice", note=f"RIB reçu : {IBAN}")
    assert review.note is not None and IBAN not in review.note
    assert ReplyReview(action="give_up", approver="alice").note is None


def test_an_answer_takes_no_unknown_field_and_is_immutable() -> None:
    with pytest.raises(ValidationError):
        ReplyReview(action="resolved", approver="alice", verdict="approve")  # type: ignore[call-arg]
    review = ReplyReview(action="resolved", approver="alice")
    with pytest.raises(ValidationError):
        review.action = "give_up"  # type: ignore[misc]


def test_asking_the_supplier_is_a_verdict_of_its_own() -> None:
    decision = ApprovalDecision(verdict=Verdict.REJECT_AND_QUERY, approver="alice")
    assert decision.verdict.value == "reject_and_query"
    assert {v.value for v in Verdict} == {"approve", "reject", "reject_and_query"}
