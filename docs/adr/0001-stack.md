# ADR-0001: Technology stack

- Status: accepted
- Date: 2026-10-21

## Context

invoice-agent processes supplier invoices with an LLM-assisted workflow that ends with a
human approval and a write to an accounting system. The project must demonstrate
production-grade engineering: reliability, traceability, testability, and a credible path
to deployment on AWS. Constraints that drive the choices:

- Workflows pause for hours or days waiting for a human, and must survive restarts.
- Write actions must be controlled, idempotent and auditable; the LLM never acts directly.
- Most of the logic is deterministic; the LLM is a component, not the architecture.
- A single developer must be able to understand and defend every part.

## Decision

| Concern | Choice |
|---|---|
| Language / tooling | Python 3.12, `uv` |
| Orchestration | LangGraph with a Postgres checkpointer and `interrupt` |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| Tool protocol | MCP server (official Python SDK) exposing the mock accounting system |
| API / UI | FastAPI + Pydantic v2; Jinja2 + HTMX for the approval queue |

### Why each choice

- **LangGraph.** The process is an explicit state machine with branches (anomaly or not),
  retries, and a pause for approval. LangGraph models this directly, persists state at each
  step, and resumes after an interrupt. It also gives a graph that can be tested path by path.
- **PostgreSQL.** One engine covers business data, LangGraph checkpoints, the audit log and
  the local job queue (polling a job table). It is available as RDS on AWS, so local and
  production stay close. Transactions give us idempotency keys via unique constraints.
- **MCP.** Write actions are exposed as typed tools behind a protocol boundary. The agent
  can only do what the server exposes, inputs are validated at that boundary, and the
  accounting system can be swapped without touching the agent. It is also a standard
  other clients can reuse.
- **FastAPI + Pydantic v2.** Typed request/response models, the same Pydantic models reused
  for structured LLM output and domain validation, async support, and automatic OpenAPI.
- **uv.** Fast, reproducible installs through a lockfile, manages the Python version, and
  is a single tool for local and CI use.
- **HTMX instead of an SPA.** The UI is a small approval queue; server-rendered pages keep
  the project focused on the backend and avoid a second toolchain.

## Alternatives considered

| Area | Rejected | Reason |
|---|---|---|
| Orchestration | Plain Python state machine | Re-implements checkpointing and resume-after-interrupt, which LangGraph already provides and tests. |
| Orchestration | CrewAI / AutoGen (multi-agent chat frameworks) | Conversation-driven control flow is hard to make deterministic and testable; our flow is a fixed graph. |
| Orchestration | Temporal | Excellent durability, but an extra service to run and operate, heavy for this scope. |
| Database | SQLite | No concurrent workers, weaker fit with the AWS target, different behaviour from production. |
| Database | MongoDB / DynamoDB | Accounting data is relational and needs transactions and constraints. |
| Queue | Redis / Celery locally | Another service to run; a Postgres job table is enough, with SQS on AWS. |
| Tool layer | Direct function calls or custom REST client | No shared protocol, validation boundary or reuse by other clients. |
| API | Flask / Django | Less native typing and async story; Django is heavier than needed. |
| Packaging | Poetry / pip-tools | Slower, and uv also manages the interpreter. |
| UI | React SPA | Second toolchain with little value for a small approval queue. |

## Consequences

- We depend on LangGraph's API and checkpointer schema, which evolve quickly; versions are
  pinned in `uv.lock` and the graph is isolated in `agent/`.
- Running MCP adds a process and a transport to operate and test, in exchange for a clear
  boundary around write actions.
- Postgres plays several roles (data, checkpoints, queue); this is simple now and may need
  to be split if load grows.
- Every new dependency still requires a stated reason (see CLAUDE.md).
