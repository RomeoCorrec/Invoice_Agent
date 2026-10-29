"""Client of the mailroom's MCP server (the simulated mail provider, ADR-0014).

The same connection as the ledger's (``mcp_connection``) with the mailroom's name and codes,
and the same two facades: ``MailroomReader`` for what can be read (the contact on file, a
conversation, the replies), ``MailroomWriter`` for the only write, ``send_message``, which is
idempotent. A node that only needs to read a reply is given a reader and cannot write to
anybody.
"""

import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from pydantic import TypeAdapter

from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.ledger import REPO_ROOT
from invoice_agent.tools.mcp_connection import CallPolicy, McpConnection
from mailroom.schemas import ContactInfo, ErrorCode, MessageInfo, SendRequest, SentMessage


def mailroom_stdio_server(database_url: str, *, read_only: bool = False) -> StdioServerParameters:
    """Parameters that start ``python -m mailroom`` as a subprocess on the given database."""
    args = ["-m", "mailroom", *(["--read-only"] if read_only else [])]
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env={"MAILROOM_DATABASE_URL": database_url},
        cwd=REPO_ROOT,
    )


class MailroomConnection(McpConnection):
    def __init__(
        self,
        server: MCPServer | StdioServerParameters,
        *,
        policy: CallPolicy | None = None,
        tracing: Tracing | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        super().__init__(
            server,
            service="mailroom",
            codes=(code.value for code in ErrorCode),
            policy=policy,
            tracing=tracing,
            **options,
        )


class MailroomReader:
    """Reads only. Give this, not the connection, to anything that must not write."""

    def __init__(self, connection: MailroomConnection) -> None:
        self._connection = connection

    async def get_contact(self, supplier_siren: str) -> ContactInfo | None:
        found = await self._connection.call("get_contact", {"supplier_siren": supplier_siren})
        return ContactInfo.model_validate(found) if found else None

    async def get_thread(self, thread_id: UUID) -> list[MessageInfo]:
        found = await self._connection.call("get_thread", {"thread_id": str(thread_id)})
        return TypeAdapter(list[MessageInfo]).validate_python(found)

    async def list_inbound(
        self, since: datetime | None = None, limit: int = 50
    ) -> list[MessageInfo]:
        arguments: dict[str, Any] = {"limit": limit}
        if since is not None:
            arguments["since"] = since.isoformat()
        found = await self._connection.call("list_inbound", arguments)
        return TypeAdapter(list[MessageInfo]).validate_python(found)


class MailroomWriter:
    """The only way to write to a supplier. Given to the sending node and to nothing else."""

    def __init__(self, connection: MailroomConnection) -> None:
        self._connection = connection

    async def send_message(self, message: SendRequest) -> SentMessage:
        found = await self._connection.call(
            "send_message", {"message": message.model_dump(mode="json")}
        )
        return SentMessage.model_validate(found)
