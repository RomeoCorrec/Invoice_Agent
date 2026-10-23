# ADR-0003: Deterministic checks and purchase-order matching

- Status: accepted
- Date: 2026-10-23

## Context

Before an invoice reaches a human it goes through checks that can be computed. Principle 1
of the project ("deterministic first, LLM second") applies: the same inputs must always give
the same anomalies, so the checks are pure Python, testable without mocks, and the LLM is
only used later for explaining them. Invoice content is untrusted, and some invoices in the
golden dataset are hostile (re-sent invoices, changed bank details, injected instructions).

## Decision

### Shape
- Rules are pure functions in `domain/rules/` (arithmetic, identifiers and bank details,
  prompt-injection tripwire, duplicate helpers). Supplier resolution and purchase-order
  matching are pure functions in `matching/`.
- `agent/checks.py::run_checks(data, context)` combines them. `ValidationContext` carries
  everything the checks need, loaded beforehand by `agent/validation.py`, which also links
  the supplier and order, moves the invoice to `validated` or `needs_review`, and appends an
  audit event in the caller's transaction. A LangGraph node will call that service.
- No anomaly leaves the invoice `validated` (still waiting for the human approval); any
  anomaly sends it to `needs_review`. The checks never approve or post anything.

### Arithmetic
VAT is the declared basis times the rate, rounded half up to the cent, compared without
tolerance. `VAT_AMOUNT_MISMATCH` covers a wrong VAT amount or a used rate missing from the
breakdown. `TOTAL_MISMATCH` covers any disagreement between lines, VAT breakdown and the
three totals. The two are independent: INV-12 has a wrong VAT but coherent totals.

### Identity and bank details
- Supplier: by SIREN, then by VAT number, never by name (free text).
- `INVALID_SIREN` covers a failed Luhn check **and** a SIREN that differs from the one on
  file for the supplier identified by VAT number (a different but well-formed SIREN would
  otherwise pass silently). It is a single anomaly per invoice, which keeps INV-19 to one.
- `IBAN_MISMATCH` compares keyed hashes; nothing is raised if either side is unknown.

### Purchase orders
- Quoted reference: the order must exist **and** belong to the recognised supplier,
  otherwise `PO_NOT_FOUND`. An unknown seller therefore cannot attach itself to a real
  supplier's order by quoting its number (fail closed).
- No reference: the single open, not yet billed order of the supplier with the same HT
  total. Zero or several candidates give `NO_PO_MATCH`; we never pick one at random.
- `PO_ALREADY_INVOICED` (nothing left, or order not open) and `AMOUNT_EXCEEDS_PO` are
  mutually exclusive, so a fully billed order is reported once.
- "Already invoiced" counts the other invoices linked to the order in the statuses
  `extracted, validated, needs_review, approved, posted`; `received`, `rejected` and
  `failed` do not consume the order.

### Duplicates
A duplicate is another invoice with the same source file, or the same invoice number
(ignoring case and punctuation) from the same seller, where "same seller" is the resolved
supplier, the same SIREN or the same VAT number. The comparison runs in both directions: a
copy received earlier always counts; a copy received later counts once it has been checked,
because it could not have flagged this one. Whatever the processing order, at least one of
two copies is flagged. Failed copies are ignored.

### Prompt injection
`PROMPT_INJECTION_SUSPECTED` is a tripwire of FR/EN patterns applied to **every string** of
the extracted invoice (collected generically, so new fields are covered), including the new
`notes` field for free text outside the lines. A hit only adds an anomaly. It does not
neutralise the text and is not a guarantee; it makes common attacks reach a human, and the
LLM prompts will still treat document text as data.

### Audit
The validation event stores the anomalies. Free text can end up in messages, so anything
that looks like an IBAN is redacted before the payload is built.

## Alternatives considered

| Topic | Rejected | Reason |
|---|---|---|
| Rules | LLM judges anomalies | Not reproducible, not testable, injectable. |
| Rounding | Tolerance of a few cents | Hides real errors; the rounding rule is specified, so be exact. |
| Supplier match | Fuzzy name matching | Easy to spoof; names are free text. |
| PO match | Pick the closest amount | A wrong guess looks like a confident match. |
| Duplicates | Unique constraint on (supplier, number) | A re-sent invoice must be stored to be flagged. |
| Duplicates | Compare only with earlier copies | Misses a copy validated before its original was extracted. |
| New code | `SIREN_MISMATCH`, `PO_SUPPLIER_MISMATCH` | Not in the dataset's 11 codes; existing codes carry the meaning. |

## Consequences

- All 21 golden invoices, processed in order, produce exactly the expected anomalies,
  supplier, order and status (integration test against a real Postgres).
- The injection patterns are a maintained list; new phrasings need new patterns and a test.
- Matching a PO by amount depends on exact equality; a partial delivery is not handled
  (listed as out of scope by the dataset).
- No per-line `quantity x unit price = amount` check: no case requires it. Add it if one does.
- Concurrency: two workers validating the same invoice are serialised by the row lock of
  `InvoiceRepository.save`. For two copies, the later one always sees the earlier one (any
  live status) and is flagged; the earlier one is flagged as well if the later was validated
  first. The only blind spot is a copy still without data and with a different source file,
  which cannot be recognised by number until it is extracted; it is then compared when it is.
