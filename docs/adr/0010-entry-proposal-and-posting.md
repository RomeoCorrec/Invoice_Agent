# ADR-0010: Proposing and posting the accounting entry

- Status: accepted
- Date: 2026-10-27

## Context

ADR-0007 gave the graph two nodes for the accounting side: one that proposes an entry and one
that posts it after approval. ADR-0008 built the ledger, whose write tool is idempotent.
Decisions remained on where the entry is computed, how the reviewer and the ledger are
guaranteed to talk about the same entry, what happens when no sound entry exists, and how
posting survives a crash between the ledger and the agent's database.

## Decision

### The entry is computed by a pure function, never by the model
`domain/accounting.propose_entry(invoice data, supplier)` returns either an `EntryProposal` or
an `EntryBlocked` with a reason:

    debit  the supplier's default charge account (one line per VAT rate)   net amount
    debit  44566 VAT deductible                                              VAT
    credit the supplier's auxiliary account (401...)                         gross amount

It refuses, with a reason, rather than guessing: unknown supplier, a currency other than EUR,
zero or negative amounts (credit notes are not booked yet), net plus VAT different from gross,
a VAT breakdown that does not add up to the totals. Every golden invoice with coherent totals
gets a balanced entry; the one with incoherent totals (INV-13) is blocked. No LLM chooses an
account; this keeps the principle "anything that can be computed is plain Python" and makes the
entry testable without mocks.

### What is shown is what is posted
`propose_entry` runs **before** the pause and stores the proposal in the run state and in the
audit log. The reviewer sees it in the approval request, and `post_entry` posts that stored
proposal, not a recomputation: a change to the supplier's accounts between the pause and the
approval cannot alter what was approved. A replayed `propose_entry` returns its first answer.

### An invoice with no entry can only be rejected
Approving an invoice that has no entry would leave it `approved` and impossible to post. The
refusal is made in `resume_run`, **before** the run is resumed: once LangGraph has consumed a
resume value it cannot be taken back, so refusing inside the node would leave the run stuck.
The reviewer sees the reason and can still reject.

### Posting
`post_entry` is reachable only through the conditional edge after an approval. It:
1. refuses to run unless the invoice is `approved` (a `posted` invoice is a replay and returns);
2. calls the ledger's `post_journal_entry` with the key `post:<invoice id>`, through the MCP
   client of ADR-0008 (timeouts, bounded retries);
3. in one transaction, stores a copy of the entry in the agent's own journal tables with the
   ledger's entry id, moves the invoice to `posted`, and appends an audit event of the tool
   call (entry id, whether it was a replay, trace id).

One key per invoice means every retry at any level is the same posting. If the process dies
after step 2 and before step 3, the next attempt gets `replayed: true` from the ledger and
completes step 3: one entry, not two. A ledger **refusal** (for example an invoice number
already booked) fails the invoice with the ledger's code and ends the run; a **transient**
failure propagates so the job is retried. If no ledger is configured the node raises: a run
that cannot post must not look finished.

### The graph stays synchronous
The ledger client is asynchronous. The node calls it with `asyncio.run`, which is safe in a
worker thread without an event loop, and opens a connection (a subprocess of the ledger over
stdio) for each posting. That costs about a second per invoice and avoids keeping a session
alive in a long-running worker; a persistent connection can be added when volume needs it.

## Alternatives considered

- **Let the model propose accounts.** More flexible for unusual invoices, but it adds an
  uncheckable decision to a step whose result moves money. A blocked entry sent to a human is
  safer than a plausible wrong account.
- **Recompute the entry at posting time.** Simpler state, but the posted entry could differ
  from the approved one.
- **Fail the approval inside the node when there is no entry.** Leaves a stuck run, as above.
- **A random key per attempt.** A retry after a crash would then rely only on the ledger's
  one-entry-per-invoice rule, turning a harmless replay into a refusal.

## Consequences

- The reviewer's screen, the audit log and the ledger agree by construction.
- Credit notes, foreign currencies and invoices with unknown suppliers need a human decision
  outside this flow until their entries are designed.
- The agent keeps a copy of each posted entry; the ledger remains the system of record.
- Posting needs the ledger's schema to be seeded and reachable: `python -m ledger seed` for a
  local run.
