# ADR-0008: The ledger as an MCP server

- Status: accepted
- Date: 2026-10-26

## Context

ADR-0007 decided that the agent reads from, and posts to, an accounting system through MCP,
with one write tool that only the posting node can use. This step builds that system and
the client the agent will use. The accounting software is a mock, but the way it is reached
should look like the real thing: separate storage, a protocol boundary, idempotent writes,
and failure modes the client has to handle.

## Decision

### Storage: its own Postgres schema
The ledger keeps its data in schema `ledger` (accounts, suppliers, purchase orders and their
lines, entries and their lines), created by migration 0005 in the same Alembic history. It
shares no table with the agent, and its tables are declared separately (`ledger/tables.py`);
a test compares them with the migration so they cannot drift.

- Posted entries and their lines are **append-only** (the trigger function of the agent's own
  journal is reused).
- A key can be used once (`UNIQUE idempotency_key`), and a supplier invoice can be booked once
  (`UNIQUE (supplier_siren, invoice_number)`), so a second posting under a new key cannot
  create a duplicate.
- Suppliers keep only the **masked IBAN**: the ledger cannot leak a full bank account because
  it does not have one.

### Tools
Read: `get_supplier` (by SIREN or VAT number), `list_open_purchase_orders`,
`get_purchase_order`, `find_invoices`, `get_chart_of_accounts` (optional prefix).
Write: `post_journal_entry(entry)`, where the entry has the idempotency key, the supplier's
SIREN, the invoice number, a date and at least two lines.

`post_journal_entry` is idempotent in the strict sense:
- the same key and the same content returns the entry already written, flagged
  `replayed: true`;
- the same key with different content is refused (`idempotency_conflict`), never overwritten;
- the comparison uses a hash of the content without the key; the write is
  `INSERT ... ON CONFLICT DO NOTHING`, so concurrent requests with one key write once (tested
  with eight threads).

The ledger also refuses an unbalanced entry (checked when the request is built, so it cannot
reach the store), an unknown supplier, and an account that is neither in the chart nor the
supplier's own auxiliary account. Every refusal has a stable code.

### MCP server, transport and exposure
The server uses the official SDK (`mcp` 2.x, `MCPServer`) over **stdio**: the client starts
`python -m ledger` as a subprocess, so there is no port and no authentication to manage
locally. Tools carry read-only / idempotent hints. `--read-only` builds a server **without the
write tool**, which is the strongest guarantee for a client that must not write. Logs go to
stderr, because stdout carries the protocol.

Anticipated refusals are raised as tool errors whose message starts with the code. The SDK
turns any other exception into a generic message, so a crash does not leak internals (tested
with a fake secret in the exception).

### Client in `invoice_agent.tools`
`LedgerConnection` owns the session and wraps every call with:
- an **explicit timeout** (10 s per call, 30 s to connect) that fires before the SDK's own;
- **bounded retries** (3 attempts, exponential backoff with jitter, a fresh session each
  time) for transient failures only: timeout, dead connection, server crash. Repeating a
  write is safe because the tool is idempotent;
- **no retry of a refusal**: it keeps its code and a message cut to 200 characters. Messages
  never contain arguments (a validation error that would echo the input is replaced);
- a Langfuse span per call, inside the invoice's trace.

Access is split in two facades, `LedgerReader` and `LedgerWriter`. The explain node receives
a reader; only the posting node receives a writer. This is the first of two layers, the second
being a read-only server for processes that need no write at all.

## Alternatives considered

- **Reusing the agent's `journal_entries` tables as the ledger.** Simplest, but it hides the
  boundary the design is about and makes "the agent posts to an external system" untrue.
- **SQLite or an in-memory ledger.** No second schema to migrate, but different locking and
  constraint behavior from production, and the concurrency guarantee could not be tested for
  real.
- **HTTP transport.** Needed for a deployed ledger, but it brings ports, authentication and
  TLS before any of that is useful. The server code is transport-independent; switching is a
  later change.
- **Exposing the write tool but trusting the prompt not to call it.** Rejected by ADR-0007.
- **A generic retry on every error.** Would repeat refusals and mask bugs.

## Consequences

- Posting is safe to retry at every level: the client, the graph node and a restarted run.
- The agent package imports the ledger's `schemas` module (the contract) from the top-level
  `ledger` package; running the ledger as a subprocess assumes the repository layout.
- One more Alembic revision and one more schema to keep in the integration fixtures
  (truncation now covers it).
- A subprocess per connection costs about a second at start-up; the connection is meant to be
  opened once per run, not per call.
- Open: the audit event per tool call is written by the graph nodes that own the database
  session; the client only exposes the trace span and the structured log.
