"""Assemble the graph.

    START -> ingest -> validate -> propose_entry -> request_approval -> apply_decision
                 \\-> END (unreadable: the invoice is already `failed`)       |
                                         approved -> post_entry -> END <-----+----> END (rejected)

``request_approval`` stops the run with ``interrupt``; the checkpointer keeps it in Postgres
until a decision resumes it. Only an approval leads to ``post_entry``.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.entry_nodes import make_post_entry, make_propose_entry
from invoice_agent.agent.graph.explain_node import make_explain
from invoice_agent.agent.graph.nodes import (
    Node,
    make_apply_decision,
    make_ingest,
    make_request_approval,
    make_validate,
)
from invoice_agent.agent.graph.state import AgentState
from invoice_agent.domain.invoice_status import InvoiceStatus


def route_after_ingest(state: AgentState) -> str:
    return "validate" if state.get("status") == InvoiceStatus.EXTRACTED.value else END


def route_after_validate(state: AgentState) -> str:
    """Anomalies are explained to the reviewer; a clean invoice goes straight on."""
    return "explain" if state.get("anomaly_codes") else "propose_entry"


def route_after_decision(state: AgentState) -> str:
    """Only an approval leads to posting; a rejection ends the run."""
    return "post_entry" if state.get("status") == InvoiceStatus.APPROVED.value else END


def traced(name: str, node: Node, deps: AgentDeps) -> Node:
    """One Langfuse span per node, in the invoice's trace."""

    def run(state: AgentState) -> dict[str, Any]:
        tracing = deps.tracing
        # Inside a worker job a span is already open and the node nests under it; when a
        # run is resumed later there is none, and the span joins the run's trace by its id.
        trace_id = None if tracing.current_trace_id() else state.get("trace_id")
        with tracing.observation(
            f"node.{name}", trace_id=trace_id, metadata={"run_id": state.get("run_id")}
        ) as span:
            try:
                return node(state)
            except GraphInterrupt:
                span.update(output="waiting for a human decision")
                raise

    return run


def build_graph(
    deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    graph = StateGraph(AgentState)
    graph.add_node("ingest", traced("ingest", make_ingest(deps), deps))
    graph.add_node("validate", traced("validate", make_validate(deps), deps))
    graph.add_node("explain", traced("explain", make_explain(deps), deps))
    graph.add_node("propose_entry", traced("propose_entry", make_propose_entry(deps), deps))
    graph.add_node(
        "request_approval", traced("request_approval", make_request_approval(deps), deps)
    )
    graph.add_node("apply_decision", traced("apply_decision", make_apply_decision(deps), deps))
    graph.add_node("post_entry", traced("post_entry", make_post_entry(deps), deps))

    graph.add_edge(START, "ingest")
    graph.add_conditional_edges("ingest", route_after_ingest, ["validate", END])
    graph.add_conditional_edges("validate", route_after_validate, ["explain", "propose_entry"])
    graph.add_edge("explain", "propose_entry")
    graph.add_edge("propose_entry", "request_approval")
    graph.add_edge("request_approval", "apply_decision")
    graph.add_conditional_edges("apply_decision", route_after_decision, ["post_entry", END])
    graph.add_edge("post_entry", END)
    return graph.compile(checkpointer=checkpointer)
