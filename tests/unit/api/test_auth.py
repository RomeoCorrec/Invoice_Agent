import re
from uuid import uuid4

import pytest

from invoice_agent.api.auth import (
    Approvers,
    AuthConfigError,
    csrf_token,
    csrf_valid,
    hash_password,
    verify_password,
)
from invoice_agent.api.config import WebConfig
from invoice_agent.config import ConfigError

SECRET = b"s" * 32


def test_a_password_is_checked_against_its_hash() -> None:
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt:16384:8:1:")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("", stored)


def test_the_hash_does_not_contain_the_password_and_changes_with_the_salt() -> None:
    first, second = hash_password("secret"), hash_password("secret")
    assert "secret" not in first and first != second
    assert verify_password("secret", first) and verify_password("secret", second)


@pytest.mark.parametrize(
    "stored",
    ["", "plain", "scrypt:1:2", "md5:1:8:1:aa:bb", "scrypt:x:8:1:aa:bb", "scrypt$x$8$1$aa$bb"],
)
def test_a_malformed_hash_never_matches_and_never_crashes(stored: str) -> None:
    assert verify_password("anything", stored) is False


def approvers() -> Approvers:
    return Approvers.parse(
        f"alice={hash_password('alice-pw')}, bob.b@example.com={hash_password('bob-pw')}"
    )


def test_only_a_listed_approver_with_the_right_password_is_authenticated() -> None:
    people = approvers()
    assert people.authenticate("alice", "alice-pw")
    assert people.authenticate("bob.b@example.com", "bob-pw")
    assert not people.authenticate("alice", "bob-pw")
    assert not people.authenticate("mallory", "alice-pw")
    assert not people.authenticate("", "")


def test_an_unknown_name_is_checked_like_a_known_one() -> None:
    """Same work for an unknown name and a wrong password: the timing does not list names."""
    import time

    people = approvers()

    def cost(name: str) -> float:
        started = time.perf_counter()
        people.authenticate(name, "wrong")
        return time.perf_counter() - started

    known, unknown = min(cost("alice") for _ in range(3)), min(cost("nobody") for _ in range(3))
    assert unknown > known / 3  # an early return would be orders of magnitude faster


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "lists nobody"),
        ("alice", "name=scrypt"),
        ("bad name=scrypt:1:2:3:4:5", "name=scrypt"),
        ("alice=plaintext", "not a scrypt hash"),
        ("alice=scrypt:1:2:3", "not a scrypt hash"),
        (f"alice={hash_password('x')},alice={hash_password('y')}", "listed twice"),
    ],
)
def test_a_bad_list_of_approvers_is_refused(value: str, message: str) -> None:
    with pytest.raises(AuthConfigError, match=message):
        Approvers.parse(value)


def test_a_csrf_token_belongs_to_one_approver_and_one_invoice() -> None:
    invoice = uuid4()
    token = csrf_token(SECRET, "alice", invoice)
    assert csrf_valid(SECRET, "alice", invoice, token)
    assert not csrf_valid(SECRET, "bob", invoice, token)
    assert not csrf_valid(SECRET, "alice", uuid4(), token)
    assert not csrf_valid(b"t" * 32, "alice", invoice, token)
    # Change the last character to a different one (one time in sixteen it already is "0").
    altered = token[:-1] + ("1" if token.endswith("0") else "0")
    assert altered != token and not csrf_valid(SECRET, "alice", invoice, altered)
    assert not csrf_valid(SECRET, "alice", invoice, "")


def test_the_web_configuration_needs_approvers_and_a_long_secret() -> None:
    good = {"APPROVERS": f"alice={hash_password('x')}", "WEB_SECRET": "x" * 32}
    config = WebConfig.from_env(good)
    assert config.approvers.authenticate("alice", "x") and config.secret == b"x" * 32
    assert "x" * 32 not in repr(config)
    with pytest.raises(ConfigError, match="lists nobody"):
        WebConfig.from_env({**good, "APPROVERS": ""})
    with pytest.raises(ConfigError, match="WEB_SECRET"):
        WebConfig.from_env({**good, "WEB_SECRET": "short"})


def test_the_hash_survives_the_readers_of_env_files() -> None:
    """uv, docker compose and dotenv libraries expand $name in values: the hash has no dollar."""
    stored = hash_password("correct horse battery")
    assert "$" not in stored
    expanded = re.sub(r"\$\{?\w+\}?", "", stored)  # what an env-file reader does to $16384
    assert expanded == stored
    assert verify_password("correct horse battery", expanded)


def test_a_hash_made_with_the_first_format_is_still_read() -> None:
    stored = hash_password("old format password")
    legacy = stored.replace(":", "$")
    assert legacy.startswith("scrypt$16384$8$1$")
    assert verify_password("old format password", legacy)
    assert not verify_password("another password", legacy)
    assert Approvers.parse(f"alice={legacy}").authenticate("alice", "old format password")


def test_a_hash_mixing_both_separators_is_refused() -> None:
    mixed = hash_password("x").replace(":", "$", 2)
    assert not verify_password("x", mixed)
    with pytest.raises(AuthConfigError, match="not a scrypt hash"):
        Approvers.parse(f"alice={mixed}")


def test_the_tampered_token_check_does_not_depend_on_the_last_character() -> None:
    for number in range(64):  # covers every last hex digit, which is what made the check flaky
        invoice = uuid4()
        token = csrf_token(SECRET, f"user{number}", invoice)
        altered = token[:-1] + ("1" if token.endswith("0") else "0")
        assert altered != token and not csrf_valid(SECRET, f"user{number}", invoice, altered)
