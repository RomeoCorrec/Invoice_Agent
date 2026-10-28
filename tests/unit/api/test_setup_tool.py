import io

import pytest

from invoice_agent.api.__main__ import main
from invoice_agent.api.auth import Approvers, verify_password
from invoice_agent.api.config import WebConfig


def test_a_hash_from_the_tool_can_be_used_as_an_approver(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert main(["hash-password"]) == 0
    stored = capsys.readouterr().out.strip()
    assert verify_password("a long enough password", stored)
    assert Approvers.parse(f"alice={stored}").authenticate("alice", "a long enough password")


def test_a_short_password_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "at least 12 characters" in captured.err


def test_the_password_is_not_accepted_on_the_command_line() -> None:
    with pytest.raises(SystemExit):
        main(["hash-password", "hunter2hunter2"])


def test_the_generated_secret_is_long_enough_and_different_each_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["secret"])
    main(["secret"])
    first, second = capsys.readouterr().out.split()
    assert first != second
    config = WebConfig.from_env(
        {"APPROVERS": f"a={_hash()}", "WEB_SECRET": first}  # accepted by the configuration
    )
    assert config.secret == first.encode()


def _hash() -> str:
    from invoice_agent.api.auth import hash_password

    return hash_password("x")


def test_the_tool_says_what_it_waits_for_when_the_input_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    assert "Reading the password from standard input" in captured.err
    assert "a long enough password" not in captured.out + captured.err
