# ADR-0012: The web approval interface

- Status: accepted
- Date: 2026-10-28

## Context

The agent pauses on every invoice until a person decides (ADR-0009), and a command-line tool
was the only way to decide. A small business needs a page: a queue of what is waiting, the
figures, the anomalies, the model's explanation and the entry that would be posted, and two
buttons. This page is the one place where a human decision enters the system, so it is
security-relevant: whoever can use it can make the ledger post an entry. It also shows text
that came from untrusted documents.

## Decision

### Server-rendered pages, FastAPI and Jinja2
Pages are rendered on the server (stack decision of ADR-0001): a queue, a page per invoice,
and a message page for errors. Every decision goes through `resume_run`, the function the
command-line tool uses, so there is one path from a human answer to the graph and its guards
(no entry, no approval; a second decision is refused). The application is created by
`create_app(config, graph, session_factory)`, which makes it testable with a real graph and
no network, and run in production with `uvicorn invoice_agent.api.main:app_from_env --factory`.

### Authentication: HTTP Basic, scrypt hashes from the environment
Approvers are listed in `APPROVERS` as `name=<scrypt hash>` (the hash is made with
`python -m invoice_agent.api hash-password`; the password is read from the terminal or a pipe,
never from the command line). Passwords are hashed with the standard library's scrypt, compared
in constant time, and an unknown name costs as much time as a wrong password. Basic
authentication means every request is authenticated and nothing is stored: no session table,
no cookie to steal or expire. Failed attempts are logged with the (cleaned) name and never the
password. The approver recorded in the audit log is the **authenticated name**, not a field of
the form: the free-text `--by` of the command line becomes a real identity.

Rejected: a login page with a signed cookie (more code and a dependency, for no gain at this
size); SSO/OIDC (the right answer for a company with an identity provider, and an extension of
this design: only the `approver` dependency changes). Limits, stated: Basic needs TLS in front
of the service when it is not on localhost; there is no rate limiting or lockout in the
application (a reverse proxy does it better); users are managed by editing the environment.

### CSRF
A browser sends Basic credentials by itself, so a hostile page could submit a form on the
approver's behalf. A decision therefore needs a token, an HMAC of the approver's name and the
invoice id under a secret (`WEB_SECRET`, at least 32 characters), put in the page when it is
rendered and checked when the decision comes back. It is stateless, bound to one approver and
one invoice (a token for another invoice or another person is refused), and a wrong token
changes nothing.

### Showing untrusted text
Every value that came from a document goes through the template's autoescaping and, first,
through the cleaning shared with the command line (control and bidirectional characters are
replaced), so a hostile supplier name is shown as text, cannot end a line early or make a
number read backwards. The model's explanation is shown under a heading that says it was
written by the model. The pages load no external resource and contain no inline script or
style, and a `Content-Security-Policy` (`default-src 'none'`, scripts, styles, images and
forms from the same origin only, no framing) enforces it; responses are not cacheable. No API
documentation or schema is served. The error pages never repeat the input.

### Decisions that cannot be taken
The page of an invoice that is not waiting says so (404); an approval for an invoice with no
entry is refused (422) and the button is not shown; a second decision on the same invoice,
from the same person double-clicking or from someone else, gets a "already decided" page (409)
and changes nothing. A failed posting ends in a message that says the invoice is marked failed.

## Alternatives considered

- **A single-page application.** The project's rule is no SPA: more code, a build step, and a
  second place to validate input.
- **Sessions in the database.** More moving parts than Basic plus a stateless token.
- **Trusting the approver name in the form.** That would make the audit log say whatever the
  browser said.
- **Putting the decision behind a JSON API first.** Nothing else consumes it yet; the form is
  the API and a JSON one can be added over the same function.

## Consequences

- A person, not a command line, now decides; the audit log names an authenticated approver.
- The service is safe to run on localhost as it is, and behind TLS elsewhere. Its secret and
  its list of approvers are configuration (`WEB_SECRET`, `APPROVERS`), validated at start-up.
- The application shares the graph and the checkpointer connection of the process; it is
  meant to run as one instance.
- Open: HTMX to refresh the queue without a reload, screenshots in the README, translation of
  the labels (they are in English, the model's explanations in French).
