import json

from sqlalchemy.exc import OperationalError

from invoice_agent.domain.iban import contains_iban
from invoice_agent.worker.runner import MAX_ERROR_LENGTH, describe_error

IBAN = "FR7699053965263585803695442"


def test_the_type_and_message_are_kept() -> None:
    assert describe_error(ValueError("bad value")) == "ValueError: bad value"


def test_an_iban_in_a_message_is_redacted() -> None:
    described = describe_error(RuntimeError(f"cannot pay {IBAN} now"))
    assert not contains_iban(described)
    assert "[IBAN redacted]" in described


def test_an_iban_cut_by_the_length_limit_does_not_survive_in_part() -> None:
    """Redacting after truncating would leave the beginning of an IBAN at the boundary."""
    padding = "x" * (MAX_ERROR_LENGTH - len("RuntimeError: ") - 10)
    described = describe_error(RuntimeError(f"{padding} {IBAN}"))
    assert len(described) <= MAX_ERROR_LENGTH
    assert "FR7699" not in described


def test_database_errors_are_reduced_to_their_types() -> None:
    error = OperationalError(
        "INSERT INTO invoices VALUES (%s)", {"payload": "secret invoice text " + IBAN}, OSError("x")
    )
    described = describe_error(error)
    assert described == "OperationalError: OSError"
    assert "secret" not in described
    assert "INSERT" not in json.dumps(described)


def test_long_messages_are_truncated() -> None:
    assert len(describe_error(RuntimeError("y" * 5000))) == MAX_ERROR_LENGTH
