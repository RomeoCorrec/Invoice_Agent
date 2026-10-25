# ADR-0006: LLM access, prompts, tracing and evaluation

- Status: accepted
- Date: 2026-10-25

## Context

Step 03 left the extraction of plain PDFs behind an interface (`InvoiceExtractor`). Step 04
fills it with a model, and has to answer four questions the project's principles raise:
how a model is called (timeouts, retries, cost, refusals), how document text is kept from
steering the model, how every call stays traceable without leaking bank details, and how a
change of model or prompt is measured instead of felt. It is a portfolio project with a
small budget (about 5 USD of model calls in total), so measurement must be cheap and
bounded.

## Decision

### One provider interface, one Anthropic implementation
`llm/provider.py` defines `LlmProvider.generate(system, user, schema, max_tokens, name)`
returning an `LlmResult` (parsed object, token usage, cost, model, latency, request id,
stop reason). `AnthropicProvider` is the only implementation; the interface exists so the
extractor, the tracing wrapper and the evaluation depend on a protocol, and so tests use a
scripted `FakeProvider` with no network.

- **Structured output through `messages.create`** with a JSON schema, then our own
  `model_validate_json`. The SDK's `messages.parse` was rejected: it validates before the
  stop reason can be read, so a refusal or a truncated answer would be reported as a schema
  error.
- **Errors are classified once**: HTTP 5xx, 408, 409, 429, 529 and connection errors are
  transient (the worker retries the job with backoff); other 4xx and refusals are permanent
  (the extraction fails). Error messages carry only the status and the error type, never the
  request or the document.
- **Explicit timeout and bounded retries** on the client, as the conventions require.
- **Cost is computed in `Decimal`** from a pricing table (`llm/pricing.py`); an unpriced
  model fails at start-up, not on the first invoice.
- **Model: Haiku 5.5 first, effort `low`.** On the golden set it is accurate enough
  (see below) and about 25 times cheaper per call than Opus. Moving up is a configuration
  change (`LLM_MODEL`) measured by the same evaluation.

### Prompts are versioned files and the document is data
`llm/prompts/extract_invoice.v1.md` is loaded with a label `name.vN@<sha8>` (a hash of the
text), recorded on every trace and evaluation run, so a result always names the exact prompt.
The document is wrapped in a `<document>` block; any `<document>` tag inside it is
neutralised so it cannot close the block. Passages addressed to an AI are copied into
`notes` by the model but are **detected by the pipeline from the raw text**, never trusted to
the model. Extracted values must also appear in the document (grounding check): an injected
instruction that changes a figure makes the extraction fail closed.

### Tracing: Langfuse, self-hosted, masked, one trace per invoice
- **Self-hosted** (compose profile `observability`): invoice data stays on the machine, no
  account or card is needed, and it can be reproduced from the repository. The cost is a
  heavy stack (web, worker, ClickHouse, Redis, MinIO, a second Postgres). Langfuse Cloud
  remains possible by changing `LANGFUSE_BASE_URL`.
- **Masking at the SDK boundary**: a `mask` function removes IBANs and API keys from every
  input, output and metadata before anything leaves the process.
- **The prompt text is not sent**, only its label; the model's input is the document text,
  which is what makes a trace useful and is masked like the rest.
- **One trace per invoice**: the ingestion job opens the trace (its id derives from the job
  id, so retries land in the same trace) and passes the id to the validation job. The trace
  id is written in the audit events, linking the audit log to the trace.
- **Tracing is optional**: without Langfuse keys, `Tracing` is a no-op and nothing breaks.
- **The Anthropic SDK's own OpenTelemetry spans are silenced**: the SDK traces every request
  through the global provider that Langfuse registers, which showed each call twice with its
  cost counted twice. The client gets a private tracer provider with no exporter and sends
  no trace context to the API. This was found by looking at a real trace, and has a test.

### Evaluation: golden dataset as a Langfuse dataset
`python -m evals.run --suite smoke|full` runs the 21 golden invoices through the production
path (extractor, injected-passage scan, grounding check, rules) and scores field accuracy,
anomaly precision and recall, purchase-order and supplier match, injection behaviour, cost
and latency. When Langfuse is configured, the run is an **experiment** on the dataset
`invoice-golden`, so runs can be compared. Spending guards: nothing is sent without `--yes`,
`--dry-run` prints a worst case, and `--max-cost` skips the remaining cases once reached.

Scoring choices worth defending: amounts are compared exactly; text ignores whitespace only;
an identifier is compared only if the ground truth defines it (found on the first real run:
the model extracted the buyer's VAT number, which the invoices print but the ground truth
omits); anomalies that need history (duplicate, order already billed) are not asked of a
single-document run.

### First measurement (full suite, Haiku 5.5, 25 Oct 2026)
Field accuracy 94.5 %, extraction accepted 20 of 21, anomaly precision 100 % and recall
84.6 % (the missing ones need history), cost 0.0097 USD for 21 invoices, latency p50 2.5 s.
The one refused case (INV-20) hides an instruction between table cells; the grounding check
rejects it, which is the intended behaviour. Occasional calls take 15 to 45 s for reasons
not yet established (provider latency or longer reasoning); this is visible in the traces.
Full table: `docs/evals/latest.md`.

## Alternatives considered

- **LangChain or an LLM gateway** for provider switching: a heavy dependency for one
  provider; the protocol is thirty lines and keeps the call path auditable.
- **Langfuse's `observe` decorators and OpenAI-style wrappers**: they hide what is sent.
  Explicit spans make the masking and the trace-per-invoice rule testable.
- **Langfuse Cloud**: simpler, but invoice text would leave the machine and it needs an
  account; kept as a configuration option.
- **Recording model responses and replaying them in CI**: cheap and deterministic, but it
  freezes the prompt; real runs stay manual and capped, scripted fakes cover the logic.
- **Setting thresholds now**: `evals/thresholds.yaml` is deliberately not written yet. One
  run on 21 cases is a baseline, not a threshold; thresholds come once the user agrees on
  them.

## Consequences

- Every model call is priced, bounded, traced and attributable to a prompt version.
- Provider outages retry the job instead of failing the invoice.
- A prompt or model change can be compared with the previous run in Langfuse.
- The golden set is small (21), so a single case moves a metric by about 5 points; the
  evaluation says what broke, it does not prove a rate.
- The local observability stack needs about 2 GB of memory; it is optional.
