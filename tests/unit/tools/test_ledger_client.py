"""The client's retry and error handling, against small in-process MCP servers (no database)."""

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from invoice_agent.tools.errors import ToolRefusedError, ToolTransientError
from invoice_agent.tools.ledger import CallPolicy, LedgerConnection, refusal_or_failure


class Recorder:
    def __init__(self) -> None:
        self.calls = 0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def connection(server: MCPServer, rec: Recorder, **policy: float) -> LedgerConnection:
    return LedgerConnection(
        server,
        policy=CallPolicy(**{"base_delay_s": 0.2, **policy}),
        sleep=rec.sleep,  # type: ignore[arg-type]
    )


def flaky_server(rec: Recorder, failures: int) -> MCPServer:
    server = MCPServer("test")

    @server.tool()
    def ping() -> str:
        rec.calls += 1
        if rec.calls <= failures:
            raise RuntimeError("database password=hunter2 is wrong")
        return "pong"

    return server


async def test_a_call_succeeds_first_time() -> None:
    rec = Recorder()
    async with connection(flaky_server(rec, 0), rec) as conn:
        assert await conn.call("ping", {}) == "pong"
    assert rec.calls == 1 and rec.sleeps == []


async def test_a_transient_failure_is_retried_with_growing_backoff() -> None:
    rec = Recorder()
    async with connection(flaky_server(rec, 2), rec, attempts=4) as conn:
        await conn.call("ping", {})
    assert rec.calls == 3
    assert len(rec.sleeps) == 2
    assert 0.1 <= rec.sleeps[0] <= 0.2 and 0.2 <= rec.sleeps[1] <= 0.4  # jittered 0.2s, 0.4s


async def test_retries_are_bounded() -> None:
    rec = Recorder()
    async with connection(flaky_server(rec, 99), rec, attempts=3) as conn:
        with pytest.raises(ToolTransientError) as failure:
            await conn.call("ping", {})
    assert rec.calls == 3 and len(rec.sleeps) == 2
    assert "hunter2" not in str(failure.value)  # a server's crash details never reach the caller


async def test_a_refusal_is_not_retried_and_keeps_its_code() -> None:
    rec = Recorder()
    server = MCPServer("test")

    @server.tool()
    def post() -> str:
        rec.calls += 1
        raise ToolError("idempotency_conflict: this idempotency key was already used")

    async with connection(server, rec) as conn:
        with pytest.raises(ToolRefusedError) as refusal:
            await conn.call("post", {})
    assert refusal.value.code == "idempotency_conflict"
    assert rec.calls == 1 and rec.sleeps == []


async def test_a_slow_call_times_out_and_is_retried() -> None:
    rec = Recorder()
    server = MCPServer("test")

    @server.tool()
    async def slow() -> str:
        rec.calls += 1
        await asyncio.sleep(5)
        return "late"

    async with connection(server, rec, attempts=2, call_timeout_s=0.2) as conn:
        with pytest.raises(ToolTransientError, match="timed out"):
            await conn.call("slow", {})
    assert rec.calls == 2


async def test_bad_arguments_are_refused_without_echoing_them() -> None:
    rec = Recorder()
    server = MCPServer("test")

    @server.tool()
    def count(n: int) -> int:
        return n

    async with connection(server, rec) as conn:
        with pytest.raises(ToolRefusedError) as refusal:
            await conn.call("count", {"n": "SECRET-INVOICE-TEXT"})
    assert refusal.value.code == "invalid_request"
    assert "SECRET-INVOICE-TEXT" not in str(refusal.value)
    assert rec.sleeps == []


async def test_an_unknown_tool_is_a_permanent_refusal() -> None:
    rec = Recorder()
    async with connection(flaky_server(rec, 0), rec) as conn:
        with pytest.raises(ToolRefusedError) as refusal:
            await conn.call("post_journal_entry", {})
    assert refusal.value.code == "unknown_tool" and rec.sleeps == []


def test_classification_keeps_a_short_message_without_the_validator_trailer() -> None:
    text = (
        "Error executing tool x: 1 validation error for xArguments\nentry\n  Value error, "
        "unbalanced_entry: debit 10.00 != credit 9.00 [type=value_error, input_value={...}]"
    )
    error = refusal_or_failure(text)
    assert isinstance(error, ToolRefusedError) and error.code == "unbalanced_entry"
    assert str(error) == "unbalanced_entry: debit 10.00 != credit 9.00"
    long = refusal_or_failure("unknown_account: " + "x" * 1000)
    assert len(str(long)) < 260
    assert isinstance(refusal_or_failure("Error executing tool x"), ToolTransientError)
