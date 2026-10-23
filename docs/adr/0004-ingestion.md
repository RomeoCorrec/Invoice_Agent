# ADR-0004: Document ingestion, hardening and extractor grounding

- Status: accepted
- Date: 2026-10-23

## Context

Invoices arrive as PDFs from outside the company. 17 of the 21 invoices of the golden
dataset embed a Factur-X (CII) XML; four are plain PDFs whose text must be turned into data
by an LLM. Both inputs are untrusted: a PDF is a container with streams, attachments and
forms that can exhaust memory or CPU, an XML can carry entities, and the text can contain
instructions aimed at the model that reads it (INV-20, INV-21). No API key is available
yet and CLAUDE.md forbids a prompt that has not been evaluated, so the real LLM provider
is not part of this step.

## Decision

### Scope
This step is the deterministic part: document checks, Factur-X parsing, PDF text
extraction, the `InvoiceExtractor` protocol (a scripted fake in tests), the grounding check
and the ingestion service. The Anthropic provider, the versioned prompt, the evaluation
runner and `evals/thresholds.yaml` are step 04, when a key is available.

### Flow (`ingestion/pipeline.py`)
1. `inspect_pdf`: size (10 MB), `%PDF-` signature, encryption, damage, page count (20).
2. Factur-X XML present: parse it with a hardened `lxml` parser. If it is broken the
   document is **refused**; falling back to the visible text would let a tampered XML hide
   behind a readable page.
3. No XML: extract bounded text, call the extractor, add the injected passages found in
   the text, then verify the result against the text (grounding). Anything else is
   `ExtractionError`, and the ingestion service stores the invoice as `failed` with a safe
   reason (never document content).

### XML
Our parser is the only one that touches the XML: `factur-x` was dropped because it parses
the attachment itself before our code can. The attachment is read with `pypdf`, and the
parser refuses any DOCTYPE, resolves no entities, loads no DTD and has no network. Amounts
are read as `Decimal` from their text, credit notes (type 381) are refused, mandatory
fields are required (no default values).

### Resource limits (decompression bombs and floods)
- every `PdfReader` is built under a cap on inflated stream sizes (object and xref streams
  are inflated at construction); the cap covers all decoders (zlib, LZW, run-length, JBIG2,
  Brotli), is context-local (thread-safe) and restored on exit;
- the attachment is decoded lazily, only if named like an invoice XML, under a 2 MB cap,
  with at most 20 names examined;
- text extraction caps a stream at 8x the text budget (at least 1 MiB), at most 10 content
  streams and 50 form invocations per page, 4 filters per stream, 500k parsing operators,
  `max_pages`, and stops as soon as the text exceeds its budget.
Tests build real bombs and assert on peak memory, so removing a cap fails a test.

### Grounding (`ingestion/grounding.py`)
An extractor's output is untrusted. Every identifier, name, date, IBAN and amount must be
printed in the document: identifiers contiguously; names, descriptions and notes as words in
order at most 12 apart (neighbouring table cells interleave); amounts as whole numeric
tokens (`2 200,00` is `2200.00`), read after IBANs are removed and capped at 8 integer
digits so that digits of an IBAN or a SIREN never pass for an amount; dates as whole tokens;
the IBAN by comparing keyed hashes with the IBANs found in the text.

### Injected passages come from the text, not from the model
Excluding the figures of an injected note from what counts as "printed" cannot depend on
the extractor declaring that note. `suspicious_passages` scans the raw text itself (a
phrase may wrap over three lines); the pipeline adds every passage to `notes`. The invoice
is then flagged by the existing injection rule (so it goes to review), and the grounding
check ignores the figures printed inside. The consistent attack (a note that prints the
fake amount, VAT and total, with an extractor that hides the note) is accepted by grounding
alone and refused with the scan, which is covered by two tests and a mutation check.

### Bank details
`IBAN_MISMATCH` also covers a note announcing new bank details or containing an IBAN
(any separator, zero-width character, full-width digit; mod-97 validated). Phrases that
tell the reader which account to use belong to the injection patterns, so INV-18 keeps one
anomaly (`IBAN_MISMATCH`) and INV-21 keeps `PROMPT_INJECTION_SUSPECTED` as the dataset expects.

### Audit
Ingestion writes `receive` and `extract` events; payloads go through `redact_payload`.
Events are read in insertion order (`seq`, migration 0003) since a step's events can share a
timestamp.

## Alternatives considered

| Topic | Rejected | Reason |
|---|---|---|
| XML | `factur-x` library | Parses the attachment before our hardened parser. |
| XML | `defusedxml` | One more dependency; hardened `lxml` is already required. |
| Broken XML | Fall back to the visible text | Lets a tampered XML hide behind a readable invoice. |
| Grounding | Substring search of values | Tables interleave cells; fabricated values slip through loose matches. |
| Injection | Trust the notes the extractor declares | A hidden note leaves its fake figures "printed". |
| Injection | New anomaly codes | The dataset's 11 codes carry the meaning; ground truth stays unchanged. |
| LLM | Provider and prompt now | No API key, and no prompt change without an evaluation run. |
| Isolation | Parse PDFs in a subprocess | Worth it at the worker step; limits cover the current risk. |

## Consequences

- The extraction of the 17 Factur-X invoices is exact and deterministic; the four plain
  PDFs depend on the extractor of step 04, and the grounding check will reject what it
  cannot find in the text.
- A PDF that itself carries injected text can fail grounding instead of reaching review,
  because the figures near the injected lines are excluded. Failing closed is acceptable.
- pypdf's pure-Python extraction has no wall-clock limit; the worker step must add a
  timeout (or a subprocess) around ingestion.
- The Factur-X XML is not compared with the visible page. A document whose XML and page
  disagree is a fraud vector worth a later check; the 21 fixtures show it is feasible.
- Amounts of 100 million euros or more cannot be grounded; far beyond the intended scale.
