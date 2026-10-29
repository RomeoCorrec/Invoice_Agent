"""What is generic in the MCP connections and what belongs to each server."""

import sys

import pytest
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from invoice_agent.tools.errors import ToolRefusedError, ToolTransientError
from invoice_agent.tools.ledger import LedgerConnection
from invoice_agent.tools.mailroom import MailroomConnection
from invoice_agent.tools.mcp_connection import CallPolicy, classify_failure, coded_pattern


def refusing_server(message: str) -> MCPServer:
    server = MCPServer("test")

    @server.tool()
    def act() -> str:
        raise ToolError(message)

    return server


async def test_each_connection_recognises_the_refusal_codes_of_its_own_server() -> None:
    async with MailroomConnection(refusing_server("recipient_not_on_file: no")) as mail:
        with pytest.raises(ToolRefusedError) as refusal:
            await mail.call("act", {})
    assert refusal.value.code == "recipient_not_on_file"

    async with LedgerConnection(refusing_server("unknown_supplier: no")) as ledger:
        with pytest.raises(ToolRefusedError) as refusal:
            await ledger.call("act", {})
    assert refusal.value.code == "unknown_supplier"


async def test_a_code_of_another_server_is_not_a_refusal_here() -> None:
    """The ledger's code means nothing to the mailroom: it is treated as a failure, not parsed."""
    policy = CallPolicy(attempts=1)
    async with MailroomConnection(refusing_server("unknown_supplier: no"), policy=policy) as mail:
        with pytest.raises(ToolTransientError):
            await mail.call("act", {})


@pytest.mark.parametrize(
    ("connection", "service"), [(LedgerConnection, "ledger"), (MailroomConnection, "mailroom")]
)
async def test_a_server_that_cannot_start_names_the_service_in_the_error(
    connection: type, service: str
) -> None:
    broken = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
    with pytest.raises(ToolTransientError, match=f"could not connect to the {service}"):
        async with connection(broken, policy=CallPolicy(attempts=1, connect_timeout_s=10)):
            pass


def test_classification_keeps_only_a_short_message_and_knows_unknown_tools() -> None:
    codes = coded_pattern(["some_code"])
    long = "x" * 500
    refusal = classify_failure(f"some_code: {long}", codes)
    assert isinstance(refusal, ToolRefusedError) and len(str(refusal)) < 300
    assert classify_failure("Unknown tool: send", codes).code == "unknown_tool"  # type: ignore[attr-defined]
    assert classify_failure("1 validation error for X", codes).code == "invalid_request"  # type: ignore[attr-defined]
    assert isinstance(classify_failure("boom", codes), ToolTransientError)
