"""The supplier correspondence: a subgraph of the invoice graph (ADR-0014).

Reached when the approver rejects an invoice and asks the supplier for a correction::

    prepare ─► review_draft ─► send_query ─► await_reply ─► review_reply ─► close
       ▲        (a person)      (a typed        (a reply or     (a person)
       │                         tool)          the deadline)
       └────────────── a reminder, or a follow-up, up to ``max_rounds`` ───────┘

Nothing leaves the company without a person having read it, the recipient is the contact on
file for the supplier (never anything the document or the model says), the reply is untrusted
text that only a person acts on, and every step is in the audit log. The state holds ids and
small results, never the text of a reply: the reviewer reads it from the mailroom.

A node that asks (``interrupt``) runs again when the answer comes, so nothing with a side
effect precedes an ``interrupt`` in the same node.
"""

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from invoice_agent.agent.correspondence.answers import DraftReview, ReplyReview, WaitEnded
from invoice_agent.agent.correspondence.drafting import make_draft
from invoice_agent.agent.correspondence.facts import build_facts
from invoice_agent.agent.correspondence.jobs import deadline_job
from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.agent.graph.nodes import STEP_VALIDATE
from invoice_agent.agent.graph.state import PauseKind
from invoice_agent.db.repositories import (
    AuditEventRepository,
    InvoiceRepository,
    JobRepository,
    SupplierRepository,
)
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.domain.rules.injection import find_injection_patterns
from invoice_agent.tools.errors import ToolRefusedError
from invoice_agent.tools.mailroom import MailroomConnection, MailroomReader, MailroomWriter
from mailroom.schemas import MessageInfo, SendRequest, SentMessage

STEP_DRAFTED = "query_drafted"
STEP_REVIEWED = "query_reviewed"
STEP_SENT = "query_sent"
STEP_REPLY = "reply_received"
STEP_REPLY_REVIEWED = "reply_reviewed"
STEP_CLOSED = "query_closed"
DRAFTER = "supplier-query-drafter"
MAILROOM_ACTOR = "mailroom"

# where the conversation stands, in ``state["query"]["status"]``
DRAFTING, APPROVED, SENT, REPLIED = "drafting", "approved", "sent", "replied"
REMINDING, AGAIN = "reminding", "again"
FINAL = frozenset(
    {
        "discarded",
        "send_refused",
        "no_reply",
        "resolved",
        "abandoned",
        "no_contact",
        "not_configured",
    }
)


class QueryState(TypedDict, total=False):
    # shared with the invoice graph
    run_id: str
    invoice_id: str
    decision: dict[str, Any]
    query: dict[str, Any]
    # private to the subgraph
    round: int
    """The number of the message being worked on, from 1."""
    reminder: bool
    draft: dict[str, Any]
    recipient: dict[str, Any]
    sent: dict[str, Any]
    wait: dict[str, Any]


Update = dict[str, Any]


class QNode(Protocol):
    """A node of the subgraph: the signature LangGraph expects (a parameter named ``state``)."""

    def __call__(self, state: QueryState) -> Update: ...


Wrap = Callable[[str, QNode], QNode]


def _where(state: QueryState, status: str, **more: Any) -> dict[str, Any]:
    return {
        "status": status,
        "round": state.get("round", 0),
        "thread_id": state["run_id"],
        **more,
    }


def _record_once(
    deps: AgentDeps,
    state: QueryState,
    step: str,
    actor: tuple[ActorType, str],
    payload: dict[str, Any],
) -> None:
    """Append an audit event unless this round already has one for this step (a replay)."""
    run_id, round_no = UUID(state["run_id"]), state.get("round", 0)
    with session_scope(deps.session_factory) as session:
        events = AuditEventRepository(session)
        if any(
            e.step == step and e.payload.get("round") == round_no
            for e in events.list_for_run(run_id)
        ):
            return
        redacted = redact_payload({**payload, "round": round_no})
        assert isinstance(redacted, dict)
        events.append(
            AuditEvent(
                id=uuid4(),
                run_id=run_id,
                step=step,
                actor_type=actor[0],
                actor_id=actor[1],
                invoice_id=UUID(state["invoice_id"]),
                payload=redacted,
                occurred_at=deps.clock(),
            )
        )


def _recorded_anomalies(deps: AgentDeps, run_id: UUID) -> list[dict[str, Any]]:
    """The findings of the checks, as the validation recorded them in this run."""
    with session_scope(deps.session_factory) as session:
        for event in AuditEventRepository(session).list_for_run(run_id):
            if event.step == STEP_VALIDATE:
                found = event.payload.get("anomalies", [])
                if isinstance(found, list):
                    return [a for a in found if isinstance(a, dict) and "code" in a]
    raise PermanentJobError("the invoice was validated outside this run")


# --- the nodes -------------------------------------------------------------------------------


def make_prepare(deps: AgentDeps) -> QNode:
    def prepare(state: QueryState) -> Update:
        """Write the message: a template, or a model's text if it passes the checks."""
        round_no = state.get("round", 0) + 1
        here = cast(QueryState, {**state, "round": round_no})
        if deps.mailroom is None:
            return {"round": round_no, "query": _where(here, "not_configured")}
        invoice_id, run_id = UUID(state["invoice_id"]), UUID(state["run_id"])
        with session_scope(deps.session_factory) as session:
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None or invoice.data is None:
                raise PermanentJobError(f"invoice {invoice_id} not found")
            supplier = (
                SupplierRepository(session).get(invoice.supplier_id)
                if invoice.supplier_id
                else None
            )
        if supplier is None or not supplier.contact_email:
            # No known supplier, or nobody to write to: the recipient is never taken from the
            # document, so there is nothing to do.
            return {"round": round_no, "query": _where(here, "no_contact")}
        facts = build_facts(
            invoice.data,
            _recorded_anomalies(deps, run_id),
            reminder=bool(state.get("reminder", False)),
        )
        drafted = make_draft(facts, provider=deps.query.provider, prompt=deps.query.prompt)
        _record_once(
            deps,
            here,
            STEP_DRAFTED,
            (ActorType.AGENT, DRAFTER),
            {
                "source": drafted.source,
                "fallback_reason": drafted.fallback_reason,
                "problems": list(drafted.problems),
                "codes": list(facts.codes),
                "reminder": facts.reminder,
                "cost_usd": str(drafted.cost_usd),
                "model": drafted.model,
            },
        )
        return {
            "round": round_no,
            "draft": {
                **drafted.draft.model_dump(mode="json"),
                "source": drafted.source,
                "fallback_reason": drafted.fallback_reason,
                "problems": list(drafted.problems),
                "edited": False,
            },
            "recipient": {"siren": supplier.siren, "address": supplier.contact_email},
            "query": _where(here, DRAFTING),
        }

    return prepare


def make_review_draft(deps: AgentDeps) -> QNode:
    def review_draft(state: QueryState) -> Update:
        """Stop until a person reads the message and sends it, edits it, or drops it."""
        draft, recipient = state["draft"], state["recipient"]
        answer = interrupt(
            {
                "kind": PauseKind.REVIEW_DRAFT.value,
                "invoice_id": state["invoice_id"],
                "round": state["round"],
                "reminder": state.get("reminder", False),
                "to": recipient["address"],
                "subject": draft["subject"],
                "body": draft["body"],
                "source": draft["source"],
                "fallback_reason": draft.get("fallback_reason"),
                "problems": draft.get("problems", []),
            }
        )
        review = DraftReview.model_validate(answer)
        edited = review.subject is not None
        _record_once(
            deps,
            state,
            STEP_REVIEWED,
            (ActorType.HUMAN, review.approver),
            {"action": review.action, "edited": edited},
        )
        if review.action == "discard":
            return {"query": _where(state, "discarded")}
        return {
            "draft": {
                **draft,
                "subject": review.subject or draft["subject"],
                "body": review.body or draft["body"],
                "edited": edited,
            },
            "query": _where(state, APPROVED),
        }

    return review_draft


def make_send_query(deps: AgentDeps) -> QNode:
    def send_query(state: QueryState) -> Update:
        """Send the reviewed message through the typed, idempotent tool: once per round."""
        assert deps.mailroom is not None
        draft, recipient = state["draft"], state["recipient"]
        request = SendRequest(
            idempotency_key=f"query:{state['run_id']}:{state['round']}",
            thread_id=UUID(state["run_id"]),
            supplier_siren=recipient["siren"],
            to=recipient["address"],
            subject=draft["subject"],
            body=draft["body"],
        )
        try:
            sent = asyncio.run(_send(deps, request))
        except ToolRefusedError as refusal:
            _record_once(
                deps,
                state,
                STEP_SENT,
                (ActorType.TOOL, MAILROOM_ACTOR),
                {"refused": refusal.code},
            )
            return {"query": _where(state, "send_refused", reason=refusal.code)}
        _record_once(
            deps,
            state,
            STEP_SENT,
            (ActorType.TOOL, MAILROOM_ACTOR),
            {"message_id": str(sent.message_id), "replayed": sent.replayed},
        )
        _schedule_deadline(deps, state)
        return {"sent": sent.model_dump(mode="json"), "query": _where(state, SENT)}

    return send_query


async def _send(deps: AgentDeps, request: SendRequest) -> SentMessage:
    assert deps.mailroom is not None
    async with MailroomConnection(
        deps.mailroom, policy=deps.mailroom_policy, tracing=deps.tracing
    ) as connection:
        return await MailroomWriter(connection).send_message(request)


def _schedule_deadline(deps: AgentDeps, state: QueryState) -> None:
    """Queue the job that ends the wait for this message (once: its key names run and round)."""
    now = deps.clock()
    with session_scope(deps.session_factory) as session:
        JobRepository(session).enqueue(
            deadline_job(
                UUID(state["run_id"]),
                state["round"],
                run_after=now + deps.query.reply_deadline,
                now=now,
            )
        )


def make_await_reply(deps: AgentDeps) -> QNode:
    def await_reply(state: QueryState) -> Update:
        """Stop until the supplier answers or the deadline passes."""
        answer = interrupt(
            {
                "kind": PauseKind.AWAIT_REPLY.value,
                "invoice_id": state["invoice_id"],
                "round": state["round"],
                "thread_id": state["run_id"],
                "message_id": state["sent"]["message_id"],
            }
        )
        ended = WaitEnded.model_validate(answer)
        if ended.event == "timeout":
            _record_once(deps, state, STEP_REPLY, (ActorType.SYSTEM, "deadline"), {"timeout": True})
            if state["round"] < deps.query.max_rounds and not state.get("reminder", False):
                return {"reminder": True, "query": _where(state, REMINDING)}
            return {"query": _where(state, "no_reply")}
        assert ended.message_id is not None
        reply = asyncio.run(_reply(deps, UUID(state["run_id"]), ended.message_id))
        flags = {
            "message_id": str(reply.id),
            "from_contact": bool(reply.from_contact),
            "iban_redacted": reply.iban_redacted,
            # The text itself is never kept in the state: only whether it looks like an attack.
            "injection_suspected": bool(find_injection_patterns([reply.subject, reply.body])),
        }
        _record_once(deps, state, STEP_REPLY, (ActorType.TOOL, MAILROOM_ACTOR), flags)
        return {"wait": flags, "query": _where(state, REPLIED)}

    return await_reply


async def _reply(deps: AgentDeps, thread_id: UUID, message_id: UUID) -> MessageInfo:
    assert deps.mailroom is not None
    async with MailroomConnection(
        deps.mailroom, policy=deps.mailroom_policy, tracing=deps.tracing
    ) as connection:
        thread = await MailroomReader(connection).get_thread(thread_id)
    for message in thread:
        if message.id == message_id and message.direction == "in":
            return message
    raise PermanentJobError("the reply is not in this conversation")


def make_review_reply(deps: AgentDeps) -> QNode:
    def review_reply(state: QueryState) -> Update:
        """Stop until a person has read the reply and says what it settles."""
        wait = state["wait"]
        answer = interrupt(
            {
                "kind": PauseKind.REVIEW_REPLY.value,
                "invoice_id": state["invoice_id"],
                "round": state["round"],
                "thread_id": state["run_id"],
                **wait,
            }
        )
        review = ReplyReview.model_validate(answer)
        _record_once(
            deps,
            state,
            STEP_REPLY_REVIEWED,
            (ActorType.HUMAN, review.approver),
            {"action": review.action, "note": review.note},
        )
        if review.action == "resolved":
            return {"query": _where(state, "resolved")}
        if review.action == "ask_again" and state["round"] < deps.query.max_rounds:
            return {"reminder": False, "query": _where(state, AGAIN)}
        return {"query": _where(state, "abandoned")}

    return review_reply


def make_close(deps: AgentDeps) -> QNode:
    def close(state: QueryState) -> Update:
        """Record how the conversation ended."""
        status = state["query"]["status"]
        final = status if status in FINAL else "abandoned"
        _record_once(
            deps,
            state,
            STEP_CLOSED,
            (ActorType.SYSTEM, "supplier-query"),
            {"status": final, "reason": state["query"].get("reason")},
        )
        return {"query": _where(state, final, reason=state["query"].get("reason"))}

    return close


# --- the routes ------------------------------------------------------------------------------


def _status(state: QueryState) -> str:
    return str(state["query"]["status"])


def route_after_prepare(state: QueryState) -> str:
    return "review_draft" if _status(state) == DRAFTING else "close"


def route_after_review(state: QueryState) -> str:
    return "send_query" if _status(state) == APPROVED else "close"


def route_after_send(state: QueryState) -> str:
    return "await_reply" if _status(state) == SENT else "close"


def route_after_wait(state: QueryState) -> str:
    status = _status(state)
    return "review_reply" if status == REPLIED else "prepare" if status == REMINDING else "close"


def route_after_reply_review(state: QueryState) -> str:
    return "prepare" if _status(state) == AGAIN else "close"


def build_query_subgraph(
    deps: AgentDeps, wrap: Wrap
) -> CompiledStateGraph[QueryState, None, QueryState, QueryState]:
    """The subgraph, compiled without a checkpointer of its own: it uses its parent's, on the
    parent's thread, so one run id and one trace cover the invoice and its correspondence."""
    graph = StateGraph(QueryState)
    graph.add_node("prepare", wrap("query.prepare", make_prepare(deps)))
    graph.add_node("review_draft", wrap("query.review_draft", make_review_draft(deps)))
    graph.add_node("send_query", wrap("query.send_query", make_send_query(deps)))
    graph.add_node("await_reply", wrap("query.await_reply", make_await_reply(deps)))
    graph.add_node("review_reply", wrap("query.review_reply", make_review_reply(deps)))
    graph.add_node("close", wrap("query.close", make_close(deps)))
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", route_after_prepare, ["review_draft", "close"])
    graph.add_conditional_edges("review_draft", route_after_review, ["send_query", "close"])
    graph.add_conditional_edges("send_query", route_after_send, ["await_reply", "close"])
    graph.add_conditional_edges(
        "await_reply", route_after_wait, ["review_reply", "prepare", "close"]
    )
    graph.add_conditional_edges("review_reply", route_after_reply_review, ["prepare", "close"])
    graph.add_edge("close", END)
    return graph.compile()
