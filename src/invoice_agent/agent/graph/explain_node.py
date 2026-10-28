"""The node where the model explains the anomalies of an invoice to the reviewer.

It is advisory. It runs only when the rules found something, it sees structured fields and
findings (never the document's free text), it may look things up in the ledger through four
read-only tools, and what it writes changes nothing: not the status, not the entry, not the
decision. If it cannot answer (provider down, refusal, invalid output, ledger unreachable) the
run goes on without an explanation and the audit log says so: a help must not fail an invoice.
"""

import json
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue

from invoice_agent.agent.explanation import Explanation, build_case, keep_known_codes
from invoice_agent.agent.graph.deps import AgentDeps, ExplainConfig
from invoice_agent.agent.graph.nodes import STEP_VALIDATE, Node
from invoice_agent.agent.graph.state import AgentState, invoice_id_of, run_id_of
from invoice_agent.config import Settings
from invoice_agent.db.repositories import AuditEventRepository, InvoiceRepository
from invoice_agent.db.session import session_scope
from invoice_agent.domain.audit import ActorType, AuditEvent, redact_payload
from invoice_agent.domain.job import PermanentJobError
from invoice_agent.llm.anthropic_provider import AnthropicProvider
from invoice_agent.llm.prompt import load_prompt
from invoice_agent.llm.provider import LlmError, LlmResult
from invoice_agent.llm.tools import ToolFailure, ToolSpec
from invoice_agent.llm.traced import TracedProvider
from invoice_agent.observability.tracing import Tracing
from invoice_agent.tools.errors import ToolError, ToolRefusedError
from invoice_agent.tools.sync_ledger import SyncLedgerReader
from ledger.schemas import BookedInvoice, PurchaseOrderInfo, SupplierInfo

STEP_EXPLAIN = "explain"
STEP_EXPLAIN_UNAVAILABLE = "explain_unavailable"
AGENT_ACTOR = "explainer"


class ReadOnlyLedger(Protocol):
    """The lookups the tools need. ``SyncLedgerReader`` is one; an evaluation can use another."""

    def get_supplier(
        self, *, siren: str | None = None, vat_number: str | None = None
    ) -> SupplierInfo | None: ...

    def list_open_purchase_orders(self, supplier_code: str) -> list[PurchaseOrderInfo]: ...

    def get_purchase_order(self, number: str) -> PurchaseOrderInfo | None: ...

    def find_invoices(
        self, supplier_siren: str, invoice_number: str | None = None
    ) -> list[BookedInvoice]: ...


def read_only_tools(reader: ReadOnlyLedger) -> list[ToolSpec]:
    """The only tools the model gets. There is no write tool in this list, by construction."""

    def text(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, list):
            return json.dumps([item.model_dump(mode="json") for item in value])
        return str(value.model_dump_json())

    def guarded(action: Any) -> Any:
        def handler(arguments: dict[str, Any]) -> str:
            try:
                return text(action(arguments))
            except ToolRefusedError as refusal:
                raise ToolFailure(f"the ledger refused the request ({refusal.code})") from None
            except ToolError:
                raise ToolFailure("the ledger is unavailable") from None

        return handler

    def need(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise ToolFailure(f"{key} is required")
        return value

    return [
        ToolSpec(
            "get_supplier",
            "Find a supplier by SIREN or VAT number. Returns null when it is not known.",
            {
                "type": "object",
                "properties": {"siren": {"type": "string"}, "vat_number": {"type": "string"}},
            },
            guarded(
                lambda a: reader.get_supplier(
                    siren=a.get("siren") or None, vat_number=a.get("vat_number") or None
                )
            ),
        ),
        ToolSpec(
            "list_open_purchase_orders",
            "List the open purchase orders of a supplier, with their lines.",
            {
                "type": "object",
                "properties": {"supplier_code": {"type": "string"}},
                "required": ["supplier_code"],
            },
            guarded(lambda a: reader.list_open_purchase_orders(need(a, "supplier_code"))),
        ),
        ToolSpec(
            "get_purchase_order",
            "Get one purchase order by its number. Returns null when it does not exist.",
            {
                "type": "object",
                "properties": {"number": {"type": "string"}},
                "required": ["number"],
            },
            guarded(lambda a: reader.get_purchase_order(need(a, "number"))),
        ),
        ToolSpec(
            "find_invoices",
            "List the invoices already booked for a supplier, optionally one invoice number.",
            {
                "type": "object",
                "properties": {
                    "supplier_siren": {"type": "string"},
                    "invoice_number": {"type": "string"},
                },
                "required": ["supplier_siren"],
            },
            guarded(
                lambda a: reader.find_invoices(
                    need(a, "supplier_siren"), a.get("invoice_number") or None
                )
            ),
        ),
    ]


def build_explain_config(
    settings: Settings, tracing: Tracing, *, model: str | None = None
) -> ExplainConfig | None:
    """The production explainer, or ``None`` when no API key is configured."""
    if settings.llm.api_key is None:
        return None
    prompt = load_prompt("explain_anomalies", 1)
    provider = TracedProvider(
        AnthropicProvider(
            api_key=settings.llm.api_key,
            model=model or settings.llm.model,
            timeout_s=settings.llm.timeout_s,
            max_retries=settings.llm.max_retries,
            effort=settings.llm.effort,
        ),
        tracing,
        prompt_label=prompt.label,
    )
    return ExplainConfig(provider=provider, prompt=prompt)


def make_explain(deps: AgentDeps) -> Node:
    def explain(state: AgentState) -> dict[str, Any]:
        config = deps.explain
        if config is None or not state.get("anomaly_codes"):
            return {"explanation": None}
        run_id, invoice_id = run_id_of(state), invoice_id_of(state)

        with session_scope(deps.session_factory) as session:
            events = AuditEventRepository(session).list_for_run(run_id)
            for event in events:
                if event.step == STEP_EXPLAIN:  # replay: the first answer stands
                    return {"explanation": event.payload.get("explanation")}
                if event.step == STEP_EXPLAIN_UNAVAILABLE:
                    return {"explanation": None}
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None or invoice.data is None:
                raise PermanentJobError(f"invoice {invoice_id} has no extracted data")
            findings = _findings(events)
        known = {str(f["code"]) for f in findings}

        try:
            result = _ask(deps, config, build_case(invoice.data, findings))
        except (LlmError, ToolError) as failure:
            _audit(
                deps,
                state,
                run_id,
                invoice_id,
                STEP_EXPLAIN_UNAVAILABLE,
                {"outcome": "unavailable", "reason": str(failure)[:300]},
            )
            return {"explanation": None}

        checked = keep_known_codes(result.parsed, known)
        # The text is model output shaped by the document: bank details are removed before it
        # reaches the state, which the checkpointer stores.
        explanation = redact_payload(checked.explanation.model_dump(mode="json"))
        assert isinstance(explanation, dict)
        _audit(
            deps,
            state,
            run_id,
            invoice_id,
            STEP_EXPLAIN,
            {
                "explanation": explanation,
                "dropped_items": checked.dropped,
                "tool_calls": [
                    {"name": c.name, "arguments": c.arguments, "ok": c.ok, "error": c.error}
                    for c in result.tool_calls
                ],
                "model": result.model,
                "prompt": config.prompt.label,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": str(result.cost_usd),
            },
        )
        return {"explanation": explanation}

    return explain


def ask_model(config: ExplainConfig, case: str, tools: list[ToolSpec]) -> LlmResult[Explanation]:
    """One bounded conversation about a case. The node and the evaluation both call this."""
    return config.provider.converse(
        system=config.prompt.text,
        user=case,
        schema=Explanation,
        tools=tools,
        max_tokens=config.max_tokens,
        name="explain_anomalies",
        max_tool_calls=config.max_tool_calls,
        max_turns=config.max_turns,
    )


def _ask(deps: AgentDeps, config: ExplainConfig, case: str) -> LlmResult[Explanation]:
    """The conversation, with the ledger's read tools when a ledger is configured."""
    if deps.ledger is None:
        return ask_model(config, case, [])
    with SyncLedgerReader(deps.ledger, policy=deps.ledger_policy, tracing=deps.tracing) as reader:
        return ask_model(config, case, read_only_tools(reader))


def _findings(events: list[AuditEvent]) -> list[dict[str, Any]]:
    for event in events:
        if event.step == STEP_VALIDATE:
            anomalies = event.payload.get("anomalies", [])
            if isinstance(anomalies, list):
                return [a for a in anomalies if isinstance(a, dict) and "code" in a]
    raise PermanentJobError("the invoice was validated outside this run")


def _audit(
    deps: AgentDeps,
    state: AgentState,
    run_id: UUID,
    invoice_id: UUID,
    step: str,
    payload: dict[str, JsonValue],
) -> None:
    if state.get("trace_id"):
        payload["trace_id"] = state["trace_id"]
    redacted = redact_payload(payload)
    assert isinstance(redacted, dict)
    with session_scope(deps.session_factory) as session:
        AuditEventRepository(session).append(
            AuditEvent(
                id=uuid4(),
                run_id=run_id,
                step=step,
                actor_type=ActorType.AGENT,
                actor_id=AGENT_ACTOR,
                invoice_id=invoice_id,
                payload=redacted,
                occurred_at=deps.clock(),
            )
        )
