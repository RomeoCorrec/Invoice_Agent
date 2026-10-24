# ADR-0005: Background worker and job queue

- Status: accepted
- Date: 2026-10-24

## Context

Ingesting and validating a document must not run inside a request: parsing can be slow, an
LLM call will be slower still, and a hostile PDF can hang the parser. The work has to survive
restarts, be retried when a dependency hiccups, never run twice at the same time for the same
job, and leave an auditable trace. ADR-0002 to ADR-0004 left three items for this step: retries
with backoff on database calls, a wall-clock limit around ingestion, and isolation of the PDF
parsing. CLAUDE.md asks for a worker polling a Postgres job table locally (SQS on AWS).

## Decision

### The queue is a Postgres table
`jobs` holds `kind`, a JSON `payload`, `status` (`queued`, `running`, `succeeded`, `dead`),
`attempts`, `max_attempts`, `run_after`, a lease (`locked_by`, `locked_until`), `last_error`
and a unique `idempotency_key`. The lifecycle is a transition table in `domain/job.py`
(tested on every pair); the database repeats the invariants (a lease exists exactly while the
job runs, attempts are bounded).

- **Claim**: `SELECT ... FOR UPDATE SKIP LOCKED` on due jobs, so concurrent workers never
  take the same row and never wait for each other. Without `SKIP LOCKED` the concurrency test
  hangs.
- **Lease**: a claimed job holds a lease of twice the job timeout. If its worker dies, the
  lease expires and another worker takes the job over; a job taken over with no attempt left
  goes `dead` instead of running again.
- **Idempotent enqueue**: a unique key (`ingest:<document>`, `validate:<invoice id>`) makes
  queueing the same thing twice a no-op.

### Three short transactions per job
1. claim and commit (the lease is now visible);
2. run the handler and mark the job succeeded in **one** transaction, so the job's effects and
   its completion are committed together or not at all;
3. on failure, roll back the work, then in a new transaction schedule a retry or mark the job
   dead.

Delivery is at least once. A worker that comes back from a long pause after its lease was
taken over fails the ownership check when it tries to complete the job, and its work is rolled
back. Tested with a hand-moved lease and mutation-checked.

### Retries
Transient failures (anything not declared permanent: database, provider, timeouts) retry with
exponential backoff: 5 s doubling to a 10 minute cap, with a jitter between half and the full
delay so retries never fire instantly and do not synchronise. After 5 attempts the job is
`dead` and stays in the table with its error. A `PermanentJobError` (unknown kind, malformed
payload, document missing from the store) goes dead at once. A document that cannot be read
is **not** a failed job: the work is done, the invoice is recorded as `failed` with a safe
reason, and no validation is queued.

### Isolating the PDF parser
`pypdf` is pure Python without a wall-clock limit, and a thread cannot be stopped. The
pipeline is split: phase 1 (`reading.py`: size and signature checks, the embedded XML, the
text) holds all the pypdf work and returns plain data; phase 2 (parse the CII, run the
extractor, ground the result) never touches the PDF again. In the worker, phase 1 runs in a
**child process** started with `spawn`, answering through a pipe, killed at the job timeout,
reporting failures by type only, with a memory ceiling (`RLIMIT_AS`) on Linux. A hang, a crash
or a memory bomb in the parser kills the child, not the worker.

### Documents
A job carries a document *name*, never the content. A `DocumentStore` resolves it; the local
implementation refuses `..`, absolute paths, NUL bytes and links leaving the root, with errors
that reveal neither paths nor contents. S3 will implement the same protocol.

### Observability
Structured JSON logs (`job_started`, `job_succeeded`, `job_retry`, `job_dead`, `lease_lost`)
with job id, kind, attempt and duration; every string goes through the IBAN redaction. Stored
errors are redacted before truncation, and database errors are reduced to their types because
their message carries the SQL parameters.

### Chaining
For now a successful ingestion queues the validation of the same invoice (same transaction,
idempotency key). The LangGraph graph of a later step will own this orchestration, along with
the human approval.

## Alternatives considered

| Topic | Rejected | Reason |
|---|---|---|
| Queue | Redis / Celery / RabbitMQ locally | Another service to run; Postgres already gives transactions, and SQS comes with AWS. |
| Claim | Optimistic `UPDATE ... WHERE status = 'queued'` | Works, but every worker contends for the same row; `SKIP LOCKED` is the standard fix. |
| Claim | Advisory locks | More moving parts for the same guarantee. |
| Completion | Mark succeeded in a separate transaction | A crash in between would redo committed work or lose it. |
| Timeout | A thread with a timer | The parser cannot be interrupted; only a process can be killed. |
| Isolation | Fork the whole job | Would carry the database connection and secrets into the child; only plain data crosses. |
| Isolation | Docker container per job | The right answer at scale; a process is enough here. |
| Retry | Fixed delay | Hammers a failing dependency and synchronises workers. |

## Consequences

- Throughput is bounded by one job at a time per worker; run several workers (the claim is
  safe) rather than threads inside one.
- Spawning a process per document costs about a second; acceptable for invoices, and the reader
  is injectable for tests.
- `RLIMIT_AS` does not exist on Windows: there the child has the deadline but no memory
  ceiling. CI and Docker run Linux.
- `ingest:<document>` as key means a corrected file must be uploaded under a new name; the same
  name is considered the same document.
- Exactly-once effects rely on the transaction: handlers must only write through the session
  they are given. A handler that calls an external service (the LLM extractor) is repeated if
  its job is retried, which is why extraction is read-only and the writes come after.
- A dead job needs a human or an operator command to be re-queued; there is none yet.
