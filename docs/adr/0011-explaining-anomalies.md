# ADR-0011: Explaining anomalies with a bounded, read-only tool loop

- Status: accepted
- Date: 2026-10-27

## Context

ADR-0007 reserved one place in the graph for the model's judgment: explaining anomalies to
the person who approves, with read-only tools. Everything else that matters is deterministic.
This step builds that node. The questions: how a model is allowed to call tools without being
able to act, how its text is kept from steering anything, what its failure costs, and how its
quality will be measured (the evaluation comes with the next step).

## Decision

### Tool use is a provider capability, bounded in three ways
`ToolProvider.converse` runs the model's tool use on the Anthropic API: the model asks for a
tool, we run it in-process, we send the result back, until it answers with the expected
schema. The bounds are enforced by the provider, not requested in the prompt:
- at most **4 tool calls** (a call beyond the budget gets an error result);
- at most **5 requests**; once the budget is spent, or on the last request, the model is told
  to answer without tools (`tool_choice: none`), and a model that still asks for a tool is
  stopped with a permanent error;
- a tool result is **cut** at 6000 characters before it returns to the model.

A tool failure is reported to the model without the exception's text (only a message the tool
chose to expose), usage and cost are summed over every request, and the whole exchange is one
Langfuse generation with a `tool` span per call. `generate` shares the request code and is
unchanged. The native API was preferred to a home-made loop where the model writes JSON
actions: the model is trained for it, the API validates the call shape, and the loop is the
shape a reviewer expects to see.

### Read-only by construction
The model is given four tools, each a thin wrapper over the ledger's lookups: `get_supplier`,
`list_open_purchase_orders`, `get_purchase_order`, `find_invoices`. The list is built from a
read-only facade; the write tool does not exist in the process that runs the loop, and a test
asserts the names offered. Arguments the model supplies are validated by the ledger, which
refuses a malformed request with a code the tool turns into a short message.

The graph is synchronous and the ledger client is not, and the client's connection must be
opened and closed by the same task. `SyncLedgerReader` keeps one connection per explanation:
a thread runs an event loop whose single session task serves calls from a queue, and each call
carries the OpenTelemetry context of its caller so its span stays in the invoice's trace.

### What the model sees and what its text can do
- It runs **only when the rules found anomalies**; a clean invoice costs no model call.
- Its input is structured: the figures and the anomalies (code, message, evidence), as JSON
  between `<case>` tags with any such tag in a value neutralised. The free text of the
  document (notes, line descriptions) is not given. The system prompt says the case is data
  and that instructions in it are not to be followed. A test feeds an extraction whose notes
  hold the sentence aimed at an AI and checks that it never reaches the model.
- Its output is advice shown to the reviewer, labelled as written by the model. It changes no
  status, no entry and no decision. Items about a code it was not given, and repeated codes,
  are dropped and counted in the audit log, so it cannot invent an anomaly.
- The text is **redacted of bank details before it enters the run state**, which the
  checkpointer stores (the lesson of the approval comment); a test scans the checkpoint tables.
- In the review command, the text is cleaned of control and bidirectional characters.

### Failure is not an invoice failure
If the provider is unavailable, refuses, returns an invalid answer, or the ledger cannot be
reached, the node records `explain_unavailable` with a safe reason and the run goes on without
an explanation. Retrying is not done at the job level: the SDK already retries transient
errors, and a help must not make a job retry or die. A replay of the node (after a crash)
reuses the recorded answer, so the model is asked once.

## Alternatives considered

- **Let the model act, with a confirmation step.** Rejected by ADR-0007; here there is simply
  no write tool in reach.
- **Pre-fetch the data instead of a tool loop.** Simpler and cheaper, and it remains the
  fallback if the loop proves not worth its cost; the evaluation will compare the two.
- **Give the model the raw text of the document.** More context for explanations, but it is
  the injection surface, and the findings already carry what an explanation needs.
- **Fail the job when the explanation fails.** Turns an optional help into a source of dead
  invoices.
- **A second model as a judge in tests.** Deferred: the first measurements are deterministic
  (codes, tool discipline, injection), which are cheap and unambiguous.

## Consequences

- A new capability in the provider that other nodes could reuse, with bounds that do not depend
  on the prompt.
- One extra model call (several requests) per invoice with anomalies; with Haiku this is
  cents for a whole evaluation set. The cost is in the audit log and in Langfuse.
- The explanation text is model output and can be wrong: it is labelled, it cannot act, and the
  deterministic findings are displayed next to it.
- Open: the evaluation (coverage, no invented code or figure, tool discipline, injection
  behavior) and its thresholds come next; the tool-using variant has not yet been measured
  against the pre-fetched one.
