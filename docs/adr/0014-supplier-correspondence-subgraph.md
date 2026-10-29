# ADR-0014: Supplier correspondence as a subgraph with durable waits

- Status: accepted
- Date: 2026-10-29

## Context

When an approver rejects an invoice (unknown order, changed bank details, duplicate...), the
natural next step is to ask the supplier for a corrected one. That is a conversation, not a
node: a draft to review, a message to send, a reply that arrives hours or days later (or never),
possibly a second round. It has four properties the rest of the graph does not have:

- **several human steps and one external event** in a row, with long waits between them;
- **an outbound action** (a message that leaves the company);
- **untrusted inbound text** (the supplier's reply, which may contain an injection, or the
  classic "our bank details have changed" fraud);
- **a time limit** (nobody answered).

The invoice graph already pauses once, for the approval (ADR-0009). The question is how to
add this without turning that graph into a flat list of twenty nodes, and without giving the
model any power it does not have today.

## Decision

1. **A subgraph of the invoice graph**, with its own state schema, added as a node. It shares
   the keys `invoice` and `outcome` with its parent and keeps its own (draft, reply, round)
   private. It runs on the invoice's thread, so there is one trace and one audit trail.
2. **Every pause carries a `kind`** (`approval`, `review_draft`, `await_reply`), and the human
   task inbox of the web interface lists tasks by kind. Everything the interface needs to show
   is in the payload, since the parent cannot see the subgraph's private state.
3. **Waits are durable and timed by the job queue**, not by a sleeping process: an
   expiry is a job with `run_after` (ADR-0005) that resumes the run with `Command(resume=...)`,
   as does the arrival of a reply. At most two rounds and one reminder.
4. **The outbound action is a typed, idempotent tool on a second MCP server** (`mailroom`, a
   simulated provider like the ledger is a simulated accounting system). The recipient comes
   from the supplier referential, never from the document and never from the model, and the
   server refuses any recipient that is not on file.
5. **Template first, model second.** A draft per anomaly code is written in plain Python and is
   both the baseline and the fallback; the model may improve its tone. A model draft is refused
   by the code if it holds a figure or an IBAN that is not in the facts it was given, a promise
   of payment, or a code that was not asked about.
6. **A reply is data.** The injection detector reads it first. The model only classifies it,
   and its answer can only send the run to a human or to the normal pipeline (a corrected
   invoice is a new invoice, with every check and its own approval). A reply that mentions bank
   details always goes to a human.

## What the spike proved

`tests/integration/test_subgraph_interrupts.py` runs a toy graph of the same shape (a parent
node, a subgraph that drafts, asks a reviewer, waits for a reply and may loop, a parent node
after it) on the installed LangGraph 1.2.18 with the Postgres checkpointer:

- the pauses of the subgraph are visible from the parent's thread with their payload (read
  the way `run_status` reads them), and the parent is paused on the subgraph node;
- `Command(resume=...)` on the parent's thread continues the subgraph where it stopped, in
  order, until the parent carries on after it;
- a reply asking for more loops to a new draft, and the loop stops at its limit;
- a run paused between two steps resumes from a **new graph object on a new connection**;
- nodes that already ran are not run again, **but the node that asked is**, so a side effect
  placed before `interrupt()` in the same node would happen twice;
- keys only the subgraph declares do not reach the parent's state;
- two threads waiting at once do not mix.

## Alternatives considered

- **A separate graph with its own thread**, started from the web interface. Simpler to resume
  and decoupled from the invoice's lifetime, but it loses the single trace and audit trail, and
  the composition (subgraph, shared and private state) is the point of the design.
- **More nodes in the one flat graph.** The state fills with fields that only three nodes use,
  and the approval path is harder to read.
- **A process that sleeps until the reply or the deadline.** Not durable: a restart loses the
  wait. The job queue already has `run_after`, retries and dead-job handling.
- **Letting the model choose the recipient, or send.** Rejected for the reason the whole project
  exists: the model never acts, and a document must not be able to choose where a message goes.
- **A real mail provider now.** Secrets, quotas and external effects in tests, for no
  gain in what the design shows. A real provider is an adapter behind the same two tools.

## Consequences

- **The node that asks runs again when the answer comes.** Nothing with a side effect goes
  before `interrupt()` in the same node, or it is idempotent (the project's nodes already are).
- A run can sit paused for days: its checkpoint rows stay, and the inbox has to show what is
  old. There is no retention policy yet.
- `rejected` stays a final status of the invoice while the conversation goes on. The
  conversation has its own state, and a corrected invoice is another invoice, linked to the
  first one.
- Resuming a nested pause needs the parent's thread id, which is the run id (ADR-0009).
- The model does more work (a draft, a classification) and so needs its own evaluation
  suite, with checks written as code like `explain` (ADR-0011).
