# invoice-agent

An agentic system that processes supplier invoices for small French businesses: ingestion (Factur-X first, PDF fallback), deterministic checks, purchase-order matching, proposed accounting entry, human approval, and posting through an MCP server, with a full audit trail.

Status: under construction. See [CLAUDE.md](CLAUDE.md) for the design principles and conventions.

## Getting started

```bash
uv sync                                       # install dependencies
uv run pre-commit install --install-hooks     # lint + conventional commit hooks
cp .env.example .env                          # then edit the values
docker compose up -d db                       # local PostgreSQL 16
uv run pytest tests/unit -q
```

Architecture decisions are recorded in [docs/adr](docs/adr). The invoice test set used for tests and
evaluation lives in `tests/fixtures/invoice-test-set/`.

## Configuration

Settings come from environment variables (see `.env.example`): `DATABASE_URL`, `IBAN_HASH_KEY`
(at least 16 characters, keeps IBANs out of the database) and optional ingestion limits.

## Processing an invoice

```text
PDF bytes -> ingest_document  (Factur-X XML parsed, or text -> extractor -> grounding check)
          -> validate_invoice (VAT, totals, SIREN, IBAN, duplicates, PO matching, injection)
          -> validated | needs_review   (a human always decides before anything is posted)
```

PDFs without an embedded XML go through an LLM extractor (Anthropic, versioned prompt, document
treated as data). See `docs/adr/0003` and `docs/adr/0004` for the rules and the ingestion
hardening, and `docs/adr/0006` for the LLM access.

## Running the worker

```bash
docker compose up -d db
uv run alembic upgrade head
cp .env.example .env                              # set DOCUMENT_ROOT and IBAN_HASH_KEY
uv run python -m invoice_agent.worker enqueue     # queue every PDF under DOCUMENT_ROOT
uv run python -m invoice_agent.worker             # process jobs until SIGINT / SIGTERM
```

Jobs live in the `jobs` table: claimed with `FOR UPDATE SKIP LOCKED`, leased, retried with
exponential backoff, and marked `dead` after the last attempt. The PDF parser runs in a child
process with a deadline. See `docs/adr/0005-worker.md`.

## Observability and evaluation

Every model call goes through `llm/`: it is timed, bounded, priced and traced in
[Langfuse](https://langfuse.com), which runs locally from the compose file.

```bash
docker compose --profile observability up -d      # Langfuse on http://localhost:3000
uv run --env-file .env python -m invoice_agent.worker
```

Set `ANTHROPIC_API_KEY` and the `LANGFUSE_*` variables in `.env` (see `.env.example`; the
compose file creates the local admin account and project from them). Without Langfuse keys
tracing is off; without an API key the worker skips PDFs that need the model.

- **One trace per invoice**, across the ingestion and validation jobs; the trace id is stored
  in the audit events. IBANs and API keys are masked before anything is sent.
- **Evaluation** on the 21 golden invoices, also run as a Langfuse experiment on the dataset
  `invoice-golden`:

```bash
uv run --env-file .env python -m evals.run --suite smoke --dry-run   # plan and worst-case cost
uv run --env-file .env python -m evals.run --suite full --yes --max-cost 0.25
```

Calling the API costs money: nothing is sent without `--yes`, and `--max-cost` stops the run.

First measurement (Haiku 5.5, 21 invoices, 0.01 USD in total): field accuracy 94.5 %,
anomaly precision 100 % and recall 84.6 % (the rest need invoice history), p50 latency
2.5 s. One document that hides an instruction between table cells is refused by the
grounding check, as intended. Details in [docs/evals/latest.md](docs/evals/latest.md) and
[ADR-0006](docs/adr/0006-llm-and-observability.md).

## The ledger and its tools

The accounting system the agent posts to is a mock, kept apart from the agent on purpose: its
own Postgres schema (`ledger`) and an MCP server that is the only way in.

```bash
uv run alembic upgrade head          # creates the ledger schema
uv run python -m ledger seed         # suppliers, purchase orders and chart of accounts
uv run python -m ledger              # MCP server over stdio (use --read-only to drop the write tool)
```

Five read tools (`get_supplier`, `list_open_purchase_orders`, `get_purchase_order`,
`find_invoices`, `get_chart_of_accounts`) and one write tool, `post_journal_entry`, which needs
an idempotency key: posting twice writes once, the same key with another content is refused,
and an invoice is booked once whatever the key. Posted entries cannot be changed.

On the agent side, `invoice_agent.tools.ledger` gives a `LedgerReader` and a `LedgerWriter`
over one connection with explicit timeouts and bounded retries. See
[ADR-0008](docs/adr/0008-ledger-mcp.md).
