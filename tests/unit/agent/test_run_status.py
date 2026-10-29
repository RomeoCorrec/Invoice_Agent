"""A paused run says what it waits for: only an approval is an approval."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from invoice_agent.agent.graph.run import RunStatus, run_status
from invoice_agent.agent.graph.state import PauseKind


def status(pending: dict[str, Any] | None) -> RunStatus:
    return RunStatus(run_id=uuid4(), finished=False, pending=pending, state={})


def test_an_approval_request_is_the_awaited_approval() -> None:
    request = {"kind": "approval", "invoice_id": "x"}
    waiting = status(request)
    assert waiting.pending_kind == "approval" and waiting.awaiting_approval == request


@pytest.mark.parametrize(
    "kind", [PauseKind.REVIEW_DRAFT, PauseKind.AWAIT_REPLY, PauseKind.REVIEW_REPLY]
)
def test_a_pause_of_another_kind_is_not_an_approval(kind: PauseKind) -> None:
    waiting = status({"kind": kind.value, "round": 1})
    assert waiting.pending_kind == kind.value
    assert waiting.awaiting_approval is None  # nobody can approve an invoice by answering it


def test_a_pause_without_a_kind_is_not_an_approval_either() -> None:
    assert status({"something": "else"}).awaiting_approval is None
    assert status(None).pending_kind is None and status(None).awaiting_approval is None


def test_the_vocabulary_of_pauses_is_the_one_of_the_adr() -> None:
    assert {kind.value for kind in PauseKind} == {
        "approval",
        "review_draft",
        "await_reply",
        "review_reply",
    }


def test_run_status_reads_the_first_pending_payload_from_the_graph_snapshot() -> None:
    interrupt = SimpleNamespace(value={"kind": "review_draft", "round": 1})
    snapshot = SimpleNamespace(
        tasks=[SimpleNamespace(interrupts=[interrupt])], values={"run_id": "r"}, next=("query",)
    )
    graph = SimpleNamespace(get_state=lambda _config: snapshot)
    found = run_status(graph, uuid4())  # type: ignore[arg-type]
    assert found.pending == {"kind": "review_draft", "round": 1}
    assert found.awaiting_approval is None and not found.finished
