# ADR-0002: Data model, accounting entries and persistence

- Status: accepted
- Date: 2026-10-21

## Context

Step 01 defines what the system stores and the rules that must always hold, whoever
writes (the agent, a worker, a human with SQL access). Constraints:

- Money is exact; accounting entries must balance.
- Invoices go through a lifecycle that includes a human decision.
- Every decision must be traceable; the audit trail must not be rewritable.
- Invoices contain bank details, the most sensitive data in a payment-fraud scenario.
- Some "invalid" invoices (wrong VAT, re-sent invoice, unknown supplier) must be stored
  precisely so they can be detected and explained.

## Decision

### Layers and mapping
Pure Pydantic models in `domain/` (immutable, no I/O); SQLAlchemy tables in `db/models.py`;
conversion written by hand in `db/mappers.py`; repositories in `db/repositories.py` accept
and return domain models only. Repositories flush but never commit: the caller owns the
transaction so that an invoice change and its audit event succeed or fail together.
Persistence is synchronous (SQLAlchemy + psycopg 3), with explicit connection and
statement timeouts.

### Money and time
`Decimal` everywhere, `Numeric(12,2)` in storage. Values with more than two decimals are
rejected by the domain, never rounded. Datetimes are timezone-aware and normalised to UTC
(`timestamptz`); the domain never reads the clock, callers pass the time in.

### Accounting entries and the balance invariant
A journal line has a `side` (debit/credit) and a strictly positive `amount`, rather than
two nullable columns, so "both set" and "both zero" cannot be represented. `JournalEntry`
is immutable and its validator enforces: at least two lines and total debit equal to total
credit. The database repeats the rule independently:

- `CHECK ((debit > 0) <> (credit > 0))` and non-negative amounts on each line;
- a `DEFERRABLE INITIALLY DEFERRED` constraint trigger checks at COMMIT that every entry
  has at least two lines and balances (deferred because lines are inserted one by one);
- entries and lines are append-only (UPDATE/DELETE rejected by trigger); a correction will
  be a new reversing entry;
- one entry per invoice (`UNIQUE`) and a unique `idempotency_key`; the repository returns
  the existing entry when the key was already used, so posting twice creates one entry.

### Invoice lifecycle
Statuses `received, extracted, validated, needs_review, approved, rejected, posted,
failed` and an explicit transition table in `domain/invoice_status.py`. A human always
decides: `posted` is only reachable from `approved`. `failed` leads only back to
`received`: a direct `failed -> approved` would allow `received -> failed -> approved` and
skip validation, and re-posting is safe thanks to the idempotency key. The repository
applies a transition with `SELECT ... FOR UPDATE` and a status check, so two workers
cannot both move the same invoice (the second gets `StaleInvoiceError`). The database
checks that extracted data is present from `extracted` onward, that `failure_reason` is
set if and only if the status is `failed`, and that statuses are valid. Statuses are text
with CHECK constraints, not native enums, so adding a value is a plain migration.

### Models validate structure, rules validate business
The models do not check that VAT or totals add up: an invoice with a wrong VAT amount or a
re-sent invoice must be storable so the rules (step 2) can flag it. Hence no unique
constraint on (supplier, invoice number), only an index.

### Bank details
Only a masked IBAN (`FR76 **** 5442`) and an HMAC-SHA256 with a secret key are stored.
`IBAN_MISMATCH` compares digests. `AuditEvent` refuses payloads containing a full IBAN.

### Audit trail
`audit_events` is append-only (trigger) and the repository has no update or delete method.
Each event has a run id, step, actor type/id, optional invoice, JSON payload, UTC timestamp.

## Alternatives considered

| Topic | Rejected | Reason |
|---|---|---|
| Entry lines | `debit` and `credit` columns in the domain | Allows both set or both zero; needs extra validation to exclude them. |
| Entry lines | Single signed amount | A sign error silently flips a line. |
| Entry storage | Lines as JSONB on the entry | No relational constraints, no CHECK, no per-account queries. |
| Entry balance | Total stored on the header | Can diverge from the lines. |
| Entry balance | Application-level check only | Does not protect against direct SQL or a future bug. |
| IBAN | Plain text | Most sensitive field if the database or a log leaks. |
| IBAN | Reversible encryption | Needs key management for no benefit: we only need equality. |
| Mapping | Automatic ORM-to-Pydantic (`from_attributes`) | Hides schema changes; domain would depend on ORM shape. |
| Concurrency | Optimistic version column | More moving parts; the row lock is simpler for a low-contention queue. |
| Async | `psycopg_async` / asyncpg | More failure modes for no gain at this scale; FastAPI runs sync routes in a thread pool. |
| Statuses | PostgreSQL ENUM | `ALTER TYPE` migrations are awkward. |

## Consequences

- The same rules are written twice (domain and SQL). A mismatch is caught by tests: the
  mapper output is committed against the triggers, and a test compares the migrated
  schema with the models.
- Append-only tables cannot be cleaned with DELETE; tests use TRUNCATE, and corrections
  go through new records.
- Amounts with more than two decimals inserted by raw SQL are rounded by PostgreSQL; the
  domain prevents it for all application writes.
- Synchronous persistence means long database calls block a worker thread; timeouts bound
  that. Revisit if the API needs high concurrency.
- Database calls do not yet have bounded retries with backoff (a convention in CLAUDE.md);
  this is left for the worker step, where the retry policy is defined.
