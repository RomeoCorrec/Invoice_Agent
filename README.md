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

The extractor for PDFs without an embedded XML is an interface (`InvoiceExtractor`); the LLM
implementation, its prompt and the evaluation come with step 04. See `docs/adr/0003` and
`docs/adr/0004` for the rules and the ingestion hardening.

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
