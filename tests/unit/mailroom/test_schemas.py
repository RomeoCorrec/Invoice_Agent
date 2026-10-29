from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mailroom.schemas import SendRequest
from mailroom.store import request_fingerprint

ADDRESS = "compta@papeterie-mercier-sarl.example"


def request(**overrides: Any) -> SendRequest:
    fields: dict[str, Any] = {
        "idempotency_key": "query:run-1:1",
        "thread_id": uuid4(),
        "supplier_siren": "151757747",
        "to": ADDRESS,
        "subject": "Facture PM-2026-0457",
        "body": "Bonjour, la commande citée sur la facture est inconnue.",
    }
    return SendRequest(**{**fields, **overrides})


def test_a_well_formed_request_is_accepted_and_its_subject_is_stripped() -> None:
    assert request(subject="  Facture PM-2026-0457  ").subject == "Facture PM-2026-0457"


@pytest.mark.parametrize(
    "subject",
    [
        "line one\nBcc: attacker@evil.example",  # a header smuggled in through the subject
        "line one\rline two",
        "",
        "   ",
        "x" * 201,
    ],
)
def test_a_subject_is_one_non_empty_line_of_bounded_length(subject: str) -> None:
    with pytest.raises(ValidationError):
        request(subject=subject)


@pytest.mark.parametrize(
    "to",
    [
        "one@domain.example, two@domain.example",
        "name@domain.example\nBcc: x@evil.example",
        "not an address",
        "name@domain",
    ],
)
def test_only_one_well_formed_recipient_is_accepted(to: str) -> None:
    with pytest.raises(ValidationError):
        request(to=to)


def test_the_body_must_have_text_and_a_bounded_length() -> None:
    for body in ("", "x" * 5001):
        with pytest.raises(ValidationError):
            request(body=body)
    assert request(body="x" * 5000)


def test_no_unknown_field_and_no_overlong_key_is_accepted() -> None:
    with pytest.raises(ValidationError):
        request(cc="someone@domain.example")
    with pytest.raises(ValidationError):
        request(idempotency_key="k" * 201)


def test_the_fingerprint_ignores_the_key_and_tells_two_messages_apart() -> None:
    thread = uuid4()
    one, same_other_key = request(thread_id=thread), request(thread_id=thread, idempotency_key="z")
    assert request_fingerprint(one) == request_fingerprint(same_other_key)
    assert request_fingerprint(one) != request_fingerprint(request(thread_id=thread, body="autre"))
    assert request_fingerprint(one) != request_fingerprint(request(thread_id=uuid4()))
