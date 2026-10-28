# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project

**invoice-agent**: an agentic system that processes supplier invoices for small French businesses.

For each incoming invoice, the system:
1. ingests it (Factur-X first, plain PDF as fallback),
2. extracts structured data,
3. runs deterministic checks (VAT arithmetic, duplicates, SIREN/SIRET format, unknown supplier),
4. matches it against open purchase orders,
5. explains anomalies and proposes an accounting entry (French PCG accounts),
6. **waits for human approval**,
7. posts the approved entry to an accounting system through an **MCP server**,
8. writes every step to an audit log.

Context: since 1 Sept 2026 every French company must be able to receive e-invoices; SMEs must issue them from 1 Sept 2027.

This is a portfolio project whose goal is to demonstrate **production-grade software engineering on an agentic system**. Code quality, tests, evaluation, observability and reliability matter more than feature count.

## Core design principles

1. **Deterministic first, LLM second.** Anything that can be computed (totals, VAT, duplicates, matching on exact keys) is plain Python. The LLM is used only for unstructured extraction, fuzzy judgment and explanations.
2. **The LLM never acts directly.** Write actions go through typed tools with validated inputs, idempotency keys, and a human approval step.
3. **Invoice content is untrusted input.** Text extracted from documents is data, never instructions. Guard against prompt injection in every prompt that includes document content.
4. **Every decision is traceable.** Each agent run produces an audit trail (inputs, tool calls, outputs, approver, timestamps) and a Langfuse trace.
5. **No feature without a test; no prompt change without an eval run.**

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12, managed with `uv` |
| API | FastAPI, Pydantic v2 |
| Orchestration | LangGraph (Postgres checkpointer, `interrupt` for human approval) |
| LLM access | Thin provider abstraction in `llm/`; Anthropic by default, Mistral as alternative |
| Tools | MCP server (official `mcp` Python SDK) exposing the mock accounting system |
| Database | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| Invoice parsing | `factur-x` library for embedded XML; `pypdf` text + LLM structured output as fallback |
| Async jobs | Local: worker process polling a Postgres job table. AWS: SQS |
| Observability | Langfuse (traces, cost, latency), structured JSON logs |
| Tests | pytest, pytest-asyncio, testcontainers (Postgres), fake LLM for unit tests |
| Quality | ruff (lint + format), mypy (strict on `src/`), pre-commit |
| CI/CD | GitHub Actions |
| Infra | Docker, docker-compose locally; Terraform on AWS (ECS Fargate, RDS Postgres, S3, SQS, Secrets Manager) |
| UI | Minimal server-rendered pages (FastAPI + Jinja2 + HTMX) for the approval queue. No SPA. |

Do not add a dependency without stating why and asking first.

## Repository layout

```
src/invoice_agent/
  domain/          # Pydantic models, business rules (pure Python, no I/O)
  db/              # SQLAlchemy models, repositories, session handling
  ingestion/       # Factur-X parsing, PDF fallback extraction
  matching/        # Purchase-order matching
  llm/             # Provider abstraction, prompts, structured-output helpers
  agent/           # LangGraph state, nodes, graph, guardrails
  tools/           # Tool definitions used by the agent (MCP client side)
  api/             # FastAPI app, routes, templates
  worker/          # Job runner
  observability/   # Langfuse setup, logging, cost tracking
ledger/            # Mock accounting service + MCP server (separate package)
evals/             # Golden dataset, generators, eval runner, metrics
infra/terraform/   # AWS infrastructure
docs/adr/          # Architecture Decision Records
tests/             # unit/, integration/, agent/
```

## Commands

```bash
uv sync                                   # install
uv run pre-commit install --install-hooks # lint + commit message hooks (once)
docker compose up -d db                    # local database
docker compose --profile observability up -d   # + Langfuse (UI on localhost:3000)
uv run alembic upgrade head               # migrations
uv run pytest                             # all tests
uv run pytest tests/unit -q               # fast tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run python -m evals.run --suite smoke  # quick eval
uv run python -m evals.run --suite full   # full eval
uv run python -m invoice_agent.api hash-password   # a password hash for APPROVERS (and: secret)
uv run uvicorn invoice_agent.api.main:app_from_env --factory   # the approval web interface
uv run python -m invoice_agent.worker enqueue   # queue the PDFs under DOCUMENT_ROOT
uv run python -m invoice_agent.worker           # run a worker (SIGINT/SIGTERM stop it cleanly)
uv run python -m invoice_agent.agent seed       # load the demo suppliers and purchase orders
uv run python -m invoice_agent.agent pending    # invoices waiting for a human decision
uv run python -m invoice_agent.agent show <invoice-id>    # the entry that would be posted
uv run python -m invoice_agent.agent approve <invoice-id> --by <name>   # or: reject
uv run python -m ledger seed                    # load the reference data into the ledger schema
uv run python -m ledger                         # serve the ledger over stdio (clients start it themselves)
```

Keep this section up to date when commands change.

## Conventions

- Code, comments, commit messages and docs in English.
- Type hints everywhere; `mypy --strict` must pass on `src/`.
- Money is `Decimal`, never `float`. Amounts are stored in cents or as `Numeric(12, 2)`.
- Dates are timezone-aware UTC in storage.
- Domain logic lives in `domain/` and has no I/O, so it can be unit-tested without mocks.
- Prompts live in versioned files under `llm/prompts/`, never inline strings scattered in code.
- Every LLM call goes through `llm/` so that tracing, retries, timeouts and cost tracking are applied uniformly.
- External calls (LLM, MCP, DB) have explicit timeouts and bounded retries with exponential backoff.
- Write tools require an idempotency key; calling them twice must not create two entries.
- Conventional commits (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`).

## Git workflow

The git history is part of the deliverable: a reviewer should be able to read how the project was built.

**Branches**
- `main` is always green (CI passing) and only changes through pull requests. Never commit or push directly to `main`, never force-push `main`.
- One branch per step or feature, created from an up-to-date `main`: `feat/<nn>-<short-name>` (e.g. `feat/03-ingestion`), `fix/<short-name>`, `test/...`, `docs/...`, `chore/...`, `refactor/...`.
- One GitHub issue per step; the PR description contains `Closes #<issue>`.

**Commits**
- Small and atomic: one logical change per commit, each commit leaves tests passing.
- Conventional commit messages with a scope: `feat(ingestion): parse Factur-X CII lines`, `test(rules): cover VAT rounding edge cases`. Imperative mood, subject ≤ 72 characters, body explains *why* when not obvious.
- Do not mix formatting-only changes with logic changes.
- Never commit generated artefacts, `.env`, local eval results or secrets.

**Before opening a PR**
- Rebase the branch on `main` (`git fetch && git rebase origin/main`); force-push is allowed only on your own feature branch, with `--force-with-lease`.
- Tidy the branch history if needed (squash "wip"/"fix typo" commits) so each commit tells part of the story.
- Run ruff, mypy, pytest; all green.
- Open the PR with `gh pr create`, using `.github/pull_request_template.md` (what, why, how tested, ADR link, screenshots or eval results if relevant).

**Merging**
- Merge with a merge commit (`gh pr merge --merge`), not squash, so the feature branches stay visible in `git log --graph`.
- Delete the branch after merge.
- Tag milestones with semver: `v0.1.0` after step 8 (usable V1), then minor bumps per step, `v1.0.0` once deployed on AWS with evals in CI.

**What you (Claude) may do without asking**: create branches, commit on a feature branch, rebase your own feature branch.
**Ask me first**: pushing, opening or merging a PR, deleting a branch, tagging, anything that rewrites shared history.

## Testing strategy

- **Unit**: domain rules, parsers, matching. No network, no DB.
- **Integration**: repositories and API against a real Postgres (testcontainers).
- **Agent**: graph paths (happy path, anomaly, rejection, tool failure, retry, resume after interrupt) with a fake LLM returning scripted outputs.
- **Evals** (in `evals/`, not in pytest): real LLM calls against the golden dataset. Metrics: field-level extraction accuracy, anomaly precision/recall, PO match accuracy, entry correctness, cost and p50/p95 latency per invoice. CI fails if a metric drops below its threshold in `evals/thresholds.yaml`.

## How to work with me (the developer)

I want to understand and be able to defend every line of this project in a technical interview. Therefore:

- **Plan before coding.** For any non-trivial task, propose a short plan (files touched, approach, trade-offs) and wait for my go-ahead.
- **Small steps.** Prefer diffs I can review in a few minutes. If a step grows beyond ~300 lines, split it.
- **Explain non-obvious choices** in your summary: what you chose, what you rejected, why.
- **Run the checks yourself** (tests, ruff, mypy) before saying a step is done, and report the actual results.
- **Never claim something works without having run it.**
- When an architectural decision is made, draft an ADR in `docs/adr/` (context, decision, alternatives, consequences).
- Do not modify existing ADRs, `evals/thresholds.yaml` or the golden dataset without asking.
- Never commit secrets. Configuration comes from environment variables (`.env` locally, Secrets Manager on AWS); keep `.env.example` updated.

## Definition of done (per step)

- [ ] Tests written and passing
- [ ] ruff and mypy clean
- [ ] Relevant docs / ADR / README section updated
- [ ] Work committed on its feature branch in atomic conventional commits, rebased on `main`
- [ ] Summary of what changed, why, and what is left
