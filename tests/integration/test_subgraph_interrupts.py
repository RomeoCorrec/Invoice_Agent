"""Spike: a subgraph that pauses several times, resumed through the Postgres checkpointer.

The supplier correspondence (ADR-0014) is a subgraph of the invoice graph that asks a human to
review a draft, then waits for a supplier's reply, possibly twice. Before building it, this
file proves on the installed LangGraph what the design relies on, with a toy graph of the same
shape:

* interruptions inside a subgraph are visible from the parent's thread, with their payload,
  so a "human task inbox" can tell them apart by a ``kind``;
* ``Command(resume=...)`` on the parent's thread continues the subgraph where it stopped,
  also from a new process (a new graph object on a new connection);
* the nodes that already ran are not run again, but the node that asked is (so a side effect
  before ``interrupt()`` must be idempotent);
* keys that only the subgraph declares stay private to it;
* two threads waiting at the same time do not mix.
"""

from collections import Counter
from collections.abc import Iterator
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from invoice_agent.agent.graph.checkpoint import open_checkpointer

MAX_ROUNDS = 2


class ParentState(TypedDict, total=False):
    invoice: str
    outcome: str
    trail: list[str]


class QueryState(TypedDict, total=False):
    # shared with the parent
    invoice: str
    outcome: str
    # private to the subgraph: the parent does not declare them
    draft: str
    reply: str
    round: int


calls: Counter[str] = Counter()


def draft(state: QueryState) -> dict[str, Any]:
    calls["draft"] += 1
    number = state.get("round", 0) + 1
    return {"draft": f"round {number} for {state['invoice']}", "round": number}


def review(state: QueryState) -> dict[str, Any]:
    calls["review"] += 1  # runs again when the answer comes: it is the node that asked
    edited = interrupt({"kind": "review_draft", "draft": state["draft"], "round": state["round"]})
    return {"draft": edited}


def wait(state: QueryState) -> dict[str, Any]:
    calls["wait"] += 1
    reply = interrupt({"kind": "await_reply", "round": state["round"]})
    return {"reply": reply}


def after_reply(state: QueryState) -> str:
    if state["reply"] == "more" and state["round"] < MAX_ROUNDS:
        return "draft"
    return "close"


def close(state: QueryState) -> dict[str, Any]:
    return {"outcome": f"closed after {state['round']} round(s): {state['reply']}"}


def build(saver: PostgresSaver) -> Any:
    sub = StateGraph(QueryState)
    sub.add_node("draft", draft)
    sub.add_node("review", review)
    sub.add_node("wait", wait)
    sub.add_node("close", close)
    sub.add_edge(START, "draft")
    sub.add_edge("draft", "review")
    sub.add_edge("review", "wait")
    sub.add_conditional_edges("wait", after_reply, ["draft", "close"])
    sub.add_edge("close", END)

    def decide(state: ParentState) -> dict[str, Any]:
        calls["decide"] += 1
        return {"trail": ["decided"]}

    def finish(state: ParentState) -> dict[str, Any]:
        calls["finish"] += 1
        return {"trail": [*state.get("trail", []), "finished"]}

    parent = StateGraph(ParentState)
    parent.add_node("decide", decide)
    parent.add_node("query", sub.compile())  # the subgraph as a node, no checkpointer of its own
    parent.add_node("finish", finish)
    parent.add_edge(START, "decide")
    parent.add_edge("decide", "query")
    parent.add_edge("query", "finish")
    parent.add_edge("finish", END)
    return parent.compile(checkpointer=saver)


def config(thread: str) -> Any:
    return {"configurable": {"thread_id": thread}}


def pending(graph: Any, thread: str) -> list[dict[str, Any]]:
    """What the run is waiting for, read the way the project reads it (``run_status``)."""
    snapshot = graph.get_state(config(thread))
    return [i.value for task in snapshot.tasks for i in task.interrupts]


@pytest.fixture(autouse=True)
def fresh_counts() -> Iterator[None]:
    calls.clear()
    yield


@pytest.fixture
def graph(saver: PostgresSaver) -> Any:
    return build(saver)


def test_the_pauses_of_a_subgraph_are_visible_from_the_parents_thread_with_their_kind(
    graph: Any,
) -> None:
    thread = str(uuid4())
    graph.invoke({"invoice": "PM-2026-0457"}, config(thread))
    [first] = pending(graph, thread)
    assert first == {"kind": "review_draft", "draft": "round 1 for PM-2026-0457", "round": 1}
    assert graph.get_state(config(thread)).next == ("query",)  # the parent is paused on the node

    graph.invoke(Command(resume="draft as edited by the reviewer"), config(thread))
    [second] = pending(graph, thread)
    assert second == {"kind": "await_reply", "round": 1}


def test_the_answers_flow_to_the_end_and_the_parent_continues_after_the_subgraph(
    graph: Any,
) -> None:
    thread = str(uuid4())
    graph.invoke({"invoice": "PM-2026-0457"}, config(thread))
    graph.invoke(Command(resume="edited"), config(thread))
    graph.invoke(Command(resume="corrected invoice attached"), config(thread))

    snapshot = graph.get_state(config(thread))
    assert not snapshot.next and pending(graph, thread) == []
    assert snapshot.values["outcome"] == "closed after 1 round(s): corrected invoice attached"
    assert snapshot.values["trail"] == ["decided", "finished"]


def test_a_reply_that_asks_for_more_loops_back_to_a_new_draft_until_the_limit(
    graph: Any,
) -> None:
    thread = str(uuid4())
    graph.invoke({"invoice": "CT-26-1188"}, config(thread))
    for _ in range(MAX_ROUNDS):
        assert pending(graph, thread)[0]["kind"] == "review_draft"
        graph.invoke(Command(resume="ok"), config(thread))
        assert pending(graph, thread)[0]["kind"] == "await_reply"
        graph.invoke(Command(resume="more"), config(thread))
    # the limit is reached: the loop stops instead of asking a third time
    assert graph.get_state(config(thread)).values["outcome"].startswith("closed after 2 round")
    assert calls["draft"] == MAX_ROUNDS


def test_a_run_paused_for_days_resumes_from_a_new_process(postgres_url: str, graph: Any) -> None:
    """A new graph object on a new connection: nothing is kept in memory."""
    thread = str(uuid4())
    graph.invoke({"invoice": "DC/2026/093"}, config(thread))
    graph.invoke(Command(resume="sent"), config(thread))
    assert pending(graph, thread)[0]["kind"] == "await_reply"

    with open_checkpointer(postgres_url) as other_connection:
        restarted = build(other_connection)
        assert pending(restarted, thread)[0]["kind"] == "await_reply"
        restarted.invoke(Command(resume="we will send a credit note"), config(thread))
        values = restarted.get_state(config(thread)).values
    assert values["outcome"] == "closed after 1 round(s): we will send a credit note"


def test_nodes_that_already_ran_are_not_run_again_but_the_node_that_asked_is(
    graph: Any,
) -> None:
    thread = str(uuid4())
    graph.invoke({"invoice": "LP-0099301"}, config(thread))
    assert (calls["decide"], calls["draft"], calls["review"]) == (1, 1, 1)
    graph.invoke(Command(resume="edited"), config(thread))
    # draft and decide were not repeated; review ran again to receive the answer
    assert (calls["decide"], calls["draft"], calls["review"], calls["wait"]) == (1, 1, 2, 1)
    graph.invoke(Command(resume="thanks"), config(thread))
    assert (calls["wait"], calls["finish"]) == (2, 1)


def test_keys_only_the_subgraph_declares_stay_out_of_the_parents_state(graph: Any) -> None:
    thread = str(uuid4())
    graph.invoke({"invoice": "IV-2026-3301"}, config(thread))
    graph.invoke(Command(resume="edited"), config(thread))
    graph.invoke(Command(resume="thanks"), config(thread))
    values = graph.get_state(config(thread)).values
    assert {"draft", "reply", "round"}.isdisjoint(values)
    assert set(values) == {"invoice", "outcome", "trail"}


def test_two_threads_waiting_at_once_do_not_mix(graph: Any) -> None:
    one, two = str(uuid4()), str(uuid4())
    graph.invoke({"invoice": "A"}, config(one))
    graph.invoke({"invoice": "B"}, config(two))
    graph.invoke(Command(resume="edited A"), config(one))  # only the first moves on
    assert pending(graph, one)[0]["kind"] == "await_reply"
    assert pending(graph, two) == [{"kind": "review_draft", "draft": "round 1 for B", "round": 1}]
    graph.invoke(Command(resume="thanks"), config(one))
    assert pending(graph, two)[0]["kind"] == "review_draft"  # still waiting for its own reviewer
