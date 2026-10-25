# ADR-0007: Agent graph and tools

- Status: proposed
- Date: 2026-10-25

## Context

Steps 02 to 05 produced the pieces: deterministic checks, purchase-order matching,
ingestion with an LLM extractor, a job worker, tracing. What is missing is the part that
joins them and that the project calls "the agent": explain anomalies, propose an accounting
entry, wait for a human, post the approved entry to the accounting system through MCP, and
leave an audit trail.

Three constraints from CLAUDE.md shape the design: anything computable is plain Python
(*deterministic first, LLM second*), the LLM never acts directly (*write actions go through
typed tools with idempotency keys and a human approval step*), and invoice content is
untrusted input. The invoice already has a state machine
(`received, extracted, validated, needs_review, approved, rejected, posted, failed`) that is
the business source of truth.

## Decision

### A fixed LangGraph graph with one judgment node
The agent is a LangGraph state graph whose edges are plain Python conditions. The model does
not choose the next step or its tools. It is used in one place, to explain anomalies.

```
START -> ingest -> validate -> match_po -+- anomalies or doubt -> explain -+
                                         +- all clear -------------------+
                                                                         v
                                                              propose_entry
                                                                         v
                                                       [interrupt: human approval]
                                         +---------- rejected ------------+---- approved ----+
                                         v                                                  v
                                   mark_rejected                                       post_entry
                                         v                                                  v
                                        END                                                END
                                                                       (any failure -> failed)
```

| Node | Nature | Role |
|---|---|---|
| `ingest` | deterministic, one LLM call for plain PDFs | Factur-X or text extraction (existing pipeline) |
| `validate` | deterministic | the eleven rules of ADR-0003 |
| `match_po` | deterministic | purchase-order matching |
| `explain` | LLM, read-only tools | plain-language explanation of each anomaly, only when there is one |
| `propose_entry` | deterministic first | balanced PCG entry (expense 6xx, input VAT 44566, supplier 401) from the supplier's default accounts; the model is consulted only when the account is ambiguous |
| `human_approval` | `interrupt` | the run stops, its state is checkpointed, the API resumes it with the decision |
| `post_entry` | MCP write tool | posts the entry with an idempotency key |

The first three nodes call the code that already exists; the graph adds orchestration, not
new rules. A human always decides, including when every check passed (this is already
encoded in the state machine: `validated` goes to `approved` or `rejected`, never straight to
`posted`).

### State and persistence
- `InvoiceState` holds identifiers and typed results (invoice id, anomalies, the proposed
  `JournalEntry`, the decision), not raw document text. The text stays in the database.
- The LangGraph Postgres checkpointer stores the run between nodes and across the
  interrupt, so a restart does not lose a run waiting for approval.
- `invoices.status` stays the business source of truth and the audit log keeps its
  append-only role. The checkpoint is only for resuming execution; no business query reads it.
- A run is keyed by invoice id, so resuming or retrying lands on the same thread.

### Tools: an MCP server for the mock accounting system (`ledger/`)
Read tools, available to the `explain` node and to deterministic nodes:
`get_supplier`, `list_open_purchase_orders`, `get_purchase_order`, `find_invoices`
(duplicates), `get_chart_of_accounts`.

Write tool, available only to `post_entry`: `post_journal_entry(entry, idempotency_key)`.
It rejects an unbalanced entry and returns the same result when called again with the same
key. The tool is simply not exposed to the model: the `explain` node's tool list contains
read tools only, and `post_entry` checks in the state that the decision is `approved` before
it calls.

Every tool validates its input with Pydantic models shared with `domain/`, has an explicit
timeout and bounded retries with backoff (transient failures retry, validation errors do
not), and writes an audit event with the trace id.

### Failures
A node failure moves the invoice to `failed` through the state machine, with the error in the
audit log, and a transient failure retries within the node first. A rejected approval ends in
`rejected` (terminal). Posting failure after approval leaves `approved`, retried by the
idempotent write, then `failed`.

### Observability and tests
Each run continues the invoice's existing Langfuse trace (the ingest and validate jobs
already share one); each node is a span, each tool call a child span, with the same masking.
Graph paths are tested with scripted fakes: happy path, anomaly, rejection, tool failure,
retry, resume after interrupt. The `explain` and `propose_entry` prompts get their own eval
metrics (entry correctness, explanation grounded in the reported anomalies) and thresholds
agreed with the owner, per "no prompt change without an eval run".

## Alternatives considered

- **A free-running tool-calling agent.** More "agentic", but non-deterministic, harder to
  test and evaluate, costlier in tokens, and it would put write tools within reach of a
  model reading hostile text. The fixed graph keeps the interesting guarantees (approval,
  idempotency) structural instead of prompted. A small read-only tool loop inside `explain`
  is the allowed way to show agent behavior.
- **Checkpoint as the only state.** Simpler, but business queries (the approval queue)
  would depend on LangGraph's internal format. Keeping `invoices.status` avoids that.
- **Posting from the API after approval, outside the graph.** Simple, but splits the audit
  trail and the retry logic in two places; the graph owns the whole flow.
- **A different orchestrator or hand-written state machine.** The state machine for the
  invoice already exists; LangGraph is used for what it adds (checkpointed interrupts, a
  visible graph), per ADR-0001.

## Consequences

- The approval requirement and the write boundary are enforced by structure and tested, not
  by prompt wording.
- Only one more LLM surface (`explain`, optionally account choice), small enough to evaluate.
- Running state exists in two places (checkpoint and `invoices.status`); the audit log and the
  status are authoritative, and a mismatch must fail closed.
- New dependencies, to be approved before the implementation step: `langgraph`,
  `langgraph-checkpoint-postgres`, `mcp`.
- Open points for the owner: whether `explain` gets a read-only tool loop or receives the
  data pre-fetched, and the eval thresholds for the new metrics.
