import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, func, select

from invoice_agent.db.reference_data import demo_contact_address
from mailroom import tables
from mailroom.seed import seed_contacts
from mailroom.server import build_server
from mailroom.store import MailroomStore

SIREN = "151757747"
ADDRESS = demo_contact_address("Papeterie Mercier SARL")
READ_TOOLS = {"get_contact", "get_thread", "list_inbound"}


@pytest.fixture
def store(engine: Engine) -> Iterator[MailroomStore]:
    seed_contacts(engine)
    yield MailroomStore(engine)


def run[T](server: MCPServer, action: Callable[[Client], Awaitable[T]]) -> T:
    async def go() -> T:
        async with Client(server) as client:
            return await action(client)

    return asyncio.run(go())


def payload(result: Any) -> Any:
    content = result.structured_content
    return content["result"] if content and set(content) == {"result"} else content


def message(thread: str, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "idempotency_key": f"query:{uuid4()}:1",
        "thread_id": thread,
        "supplier_siren": SIREN,
        "to": ADDRESS,
        "subject": "Facture PM-2026-0457",
        "body": "Bonjour, la commande citée est inconnue.",
    }
    return {"message": {**fields, **overrides}}


def stored(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.messages)).scalar_one()


def test_the_server_exposes_three_reads_and_one_send_and_no_way_to_receive(
    store: MailroomStore,
) -> None:
    async def names(client: Client) -> dict[str, Any]:
        return {tool.name: tool for tool in (await client.list_tools()).tools}

    tools = run(build_server(store), names)
    assert set(tools) == READ_TOOLS | {"send_message"}  # nothing lets an agent forge a reply
    assert all(tools[name].annotations.read_only_hint for name in READ_TOOLS)
    send = tools["send_message"].annotations
    assert not send.read_only_hint and send.idempotent_hint and not send.destructive_hint
    assert send.open_world_hint  # the message leaves the company


def test_a_read_only_server_has_no_send_tool_and_refuses_the_call(
    store: MailroomStore, engine: Engine
) -> None:
    async def attempt(client: Client) -> tuple[set[str], Any]:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        return tools, await client.call_tool("send_message", message(str(uuid4())))

    tools, result = run(build_server(store, writable=False), attempt)
    assert tools == READ_TOOLS
    assert result.is_error and stored(engine) == 0


def test_sending_twice_through_the_server_writes_once(store: MailroomStore, engine: Engine) -> None:
    thread = str(uuid4())
    arguments = message(thread)

    async def twice(client: Client) -> tuple[Any, Any, Any]:
        first = await client.call_tool("send_message", arguments)
        second = await client.call_tool("send_message", arguments)
        return (
            payload(first),
            payload(second),
            await client.call_tool("get_thread", {"thread_id": thread}),
        )

    first, second, conversation = run(build_server(store), twice)
    assert first["replayed"] is False and second["replayed"] is True
    assert first["message_id"] == second["message_id"] and stored(engine) == 1
    [only] = payload(conversation)
    assert only["direction"] == "out" and only["address"] == ADDRESS


def test_a_refusal_reaches_the_client_as_a_tool_error_with_its_code(
    store: MailroomStore, engine: Engine
) -> None:
    async def attempt(client: Client) -> Any:
        return await client.call_tool(
            "send_message", message(str(uuid4()), to="attacker@evil.example")
        )

    result = run(build_server(store), attempt)
    assert result.is_error
    assert "recipient_not_on_file" in result.content[0].text and stored(engine) == 0


def test_a_malformed_message_is_refused_before_it_reaches_the_store(
    store: MailroomStore, engine: Engine
) -> None:
    async def attempt(client: Client) -> Any:
        header = "Facture\nBcc: attacker@evil.example"
        return await client.call_tool("send_message", message(str(uuid4()), subject=header))

    assert run(build_server(store), attempt).is_error and stored(engine) == 0


def test_replies_are_visible_to_the_agent_but_cannot_be_created_by_it(
    store: MailroomStore,
) -> None:
    thread = uuid4()
    run(
        build_server(store),
        lambda client: client.call_tool("send_message", message(str(thread))),
    )
    reply = store.receive_message(thread, ADDRESS, "Re:", "Voici la facture corrigée")

    async def reads(client: Client) -> tuple[Any, Any, Any]:
        contact = await client.call_tool("get_contact", {"supplier_siren": SIREN})
        inbound = await client.call_tool("list_inbound", {})
        bad = await client.call_tool("get_thread", {"thread_id": "not-a-uuid"})
        return payload(contact), payload(inbound), bad

    contact, inbound, bad = run(build_server(store), reads)
    assert contact["address"] == ADDRESS
    assert [m["id"] for m in inbound] == [str(reply.id)] and inbound[0]["from_contact"] is True
    assert bad.is_error and "invalid_request" in bad.content[0].text
