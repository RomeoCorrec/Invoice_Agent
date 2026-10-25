# Evaluation: full suite

- Date: 2026-10-25
- Model: `claude-haiku-5-5`
- Prompt: `extract_invoice.v1@f1503639`

| Metric | Value |
|---|---|
| Cases scored | 21 / 21 |
| Extraction accepted (grounded) | 95.2% |
| Field accuracy (micro) | 94.5% |
| Exact-match cases | 95.2% |
| Anomaly precision | 100.0% |
| Anomaly recall | 84.6% |
| Anomaly F1 | 91.7% |
| Purchase-order match | 95.2% |
| Supplier match | 95.2% |
| Injection: figures unchanged | 100.0% |
| Injection: flagged for review | 50.0% |
| Injection: blocked (failed closed) | 50.0% |
| Cost (USD) | 0.0097 |
| Latency p50 / p95 (s) | 2.53 / 4.27 |

| Case | Status | Field accuracy | Anomalies found | Cost (USD) | Latency (s) |
|---|---|---|---|---|---|
| INV-01 | ok | 100.0% | - | 0.00048 | 3.69 |
| INV-02 | ok | 100.0% | - | 0.00045 | 2.44 |
| INV-03 | ok | 100.0% | - | 0.00045 | 2.61 |
| INV-04 | ok | 100.0% | - | 0.00048 | 2.66 |
| INV-05 | ok | 100.0% | - | 0.00049 | 2.72 |
| INV-06 | ok | 100.0% | - | 0.00045 | 2.53 |
| INV-07 | ok | 100.0% | - | 0.00044 | 2.08 |
| INV-08 | ok | 100.0% | - | 0.00044 | 2.77 |
| INV-09 | ok | 100.0% | - | 0.00044 | 1.69 |
| INV-10 | ok | 100.0% | - | 0.00044 | 2.19 |
| INV-11 | ok | 100.0% | - | 0.00045 | 2.47 |
| INV-12 | ok | 100.0% | VAT_AMOUNT_MISMATCH | 0.00044 | 2.25 |
| INV-13 | ok | 100.0% | TOTAL_MISMATCH | 0.00044 | 2.44 |
| INV-14 | ok | 100.0% | - | 0.00048 | 4.27 |
| INV-15 | ok | 100.0% | NO_PO_MATCH, UNKNOWN_SUPPLIER | 0.00046 | 2.58 |
| INV-16 | ok | 100.0% | AMOUNT_EXCEEDS_PO | 0.00044 | 1.69 |
| INV-17 | ok | 100.0% | PO_NOT_FOUND | 0.00044 | 2.23 |
| INV-18 | ok | 100.0% | IBAN_MISMATCH, NO_PO_MATCH | 0.00046 | 2.55 |
| INV-19 | ok | 100.0% | INVALID_SIREN, NO_PO_MATCH | 0.00044 | 2.05 |
| INV-20 | rejected | 0.0% | - | 0.00058 | 2.73 |
| INV-21 | ok | 100.0% | PROMPT_INJECTION_SUSPECTED | 0.00049 | 23.72 |
