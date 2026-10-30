# Synthetic supplier replies

Fifteen invented replies of a supplier to a query about invoice INV-12 (ADR-0014). They are
written by hand, in French (one in English), and contain no real person, address or account:
the addresses end in `.example` and the only account number is the public test IBAN used
elsewhere in the tests.

| Category | Cases | Meaning |
|---|---|---|
| `settles` | R-01 to R-04 | The reply answers the query: a person would mark it resolved. |
| `incomplete` | R-05 to R-07 | Partial, vague or a question back: ask again. |
| `ends` | R-08 to R-10 | A refusal, an automatic reply, or noise: nothing more to expect. |
| `risk` | R-11 to R-15 | A bank account in the text, an instruction aimed at an AI, a sender that is not the address on file, a request to change bank details. |

`expected.from_contact`, `iban_redacted` and `injection_suspected` are what the deterministic
checks must report; `tests/integration/test_reply_dataset.py` runs the real mailroom and detector
on every case and fails if the dataset and the code disagree. `expected.action` lists the
actions a person may reasonably take (any one is correct). The agent never takes the action
itself: a person qualifies every reply.

`known_gap` marks a case the code does not detect on its own (R-15): it is kept because the
dataset should show what is not covered.

The cases are meant for the future evaluation of the correspondence (step 13k). Do not edit them
without agreement, as for the invoice golden dataset.
