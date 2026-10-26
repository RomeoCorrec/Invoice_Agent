import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, func, select

from ledger import tables
from ledger.schemas import PostedEntry, SupplierInfo
from ledger.seed import seed_reference
from ledger.server import build_server
from ledger.store import LedgerStore
from tests.unit.ledger.test_schemas import lines, request

READ_TOOLS = {
    "get_supplier",
    "list_open_purchase_orders",
    "get_purchase_order",
    "find_invoices",
    "get_chart_of_accounts",
}


@pytest.fixture
def store(engine: Engine) -> Iterator[LedgerStore]:
    seed_reference(engine)
    yield LedgerStore(engine)


def run[T](server: MCPServer, action: Callable[[Client], Awaitable[T]]) -> T:
    async def go() -> T:
        async with Client(server) as client:
            return await action(client)

    return asyncio.run(go())


def payload(result: Any) -> Any:
    content = result.structured_content
    return content["result"] if content and set(content) == {"result"} else content


def entries(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(tables.entries)).scalar_one()


def test_the_server_exposes_five_reads_and_one_write(store: LedgerStore) -> None:
    async def names(client: Client) -> dict[str, Any]:
        return {tool.name: tool for tool in (await client.list_tools()).tools}

    tools = run(build_server(store), names)
    assert set(tools) == READ_TOOLS | {"post_journal_entry"}
    assert all(tools[name].annotations.read_only_hint for name in READ_TOOLS)
    write = tools["post_journal_entry"].annotations
    assert not write.read_only_hint and write.idempotent_hint and not write.destructive_hint


def test_a_read_only_server_has_no_write_tool_and_refuses_the_call(
    store: LedgerStore, engine: Engine
) -> None:
    async def attempt(client: Client) -> tuple[set[str], Any]:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        arguments = {"entry": request().model_dump(mode="json")}
        return tools, await client.call_tool("post_journal_entry", arguments)

    tools, result = run(build_server(store, writable=False), attempt)
    assert tools == READ_TOOLS
    assert result.is_error
    assert entries(engine) == 0


def test_reads_return_typed_data(store: LedgerStore) -> None:
    async def look(client: Client) -> tuple[Any, Any, Any]:
        return (
            await client.call_tool("get_supplier", {"siren": "151757747"}),
            await client.call_tool("get_supplier", {"siren": "000000000"}),
            await client.call_tool("list_open_purchase_orders", {"supplier_code": "SUP-001"}),
        )

    found, missing, orders = run(build_server(store), look)
    assert SupplierInfo.model_validate(payload(found)).code == "SUP-001"
    assert not missing.is_error and payload(missing) is None
    assert next(o["number"] for o in payload(orders)) == "PO-2026-0101"


def test_a_lookup_without_a_key_is_a_coded_error(store: LedgerStore) -> None:
    result = run(build_server(store), lambda c: c.call_tool("get_supplier", {}))
    assert result.is_error
    assert "invalid_request: give a siren" in result.content[0].text


def test_posting_twice_writes_once_and_says_so(store: LedgerStore, engine: Engine) -> None:
    arguments = {"entry": request().model_dump(mode="json")}

    async def twice(client: Client) -> tuple[Any, Any]:
        return (
            await client.call_tool("post_journal_entry", arguments),
            await client.call_tool("post_journal_entry", arguments),
        )

    first, second = run(build_server(store), twice)
    a, b = PostedEntry.model_validate(payload(first)), PostedEntry.model_validate(payload(second))
    assert (a.replayed, b.replayed) == (False, True) and a.entry_id == b.entry_id
    assert entries(engine) == 1


def test_a_refusal_reaches_the_client_with_its_code(store: LedgerStore, engine: Engine) -> None:
    changed = request(lines=lines("250.00", "50.00", "300.00"))

    async def conflict(client: Client) -> Any:
        await client.call_tool("post_journal_entry", {"entry": request().model_dump(mode="json")})
        return await client.call_tool(
            "post_journal_entry", {"entry": changed.model_dump(mode="json")}
        )

    result = run(build_server(store), conflict)
    assert result.is_error
    assert "idempotency_conflict:" in result.content[0].text
    assert entries(engine) == 1


def test_an_unbalanced_entry_is_rejected_before_it_reaches_the_store(
    store: LedgerStore, engine: Engine
) -> None:
    raw = request().model_dump(mode="json")
    raw["lines"][2]["amount"] = "298.79"
    result = run(build_server(store), lambda c: c.call_tool("post_journal_entry", {"entry": raw}))
    assert result.is_error and "unbalanced_entry" in result.content[0].text
    assert entries(engine) == 0


def test_an_unexpected_crash_does_not_leak_its_details(store: LedgerStore) -> None:
    def explode(**_kwargs: Any) -> None:
        raise RuntimeError("password=hunter2 host=db.internal")

    store.get_supplier = explode  # type: ignore[method-assign]
    result = run(build_server(store), lambda c: c.call_tool("get_supplier", {"siren": "151757747"}))
    assert result.is_error
    assert "hunter2" not in result.content[0].text and "db.internal" not in result.content[0].text
