# ADR-0013: Submitting invoices from the web interface, and following them

- Status: accepted
- Date: 2026-10-28

## Context

Until now an invoice entered the system through `python -m invoice_agent.worker enqueue`, which
queues the PDFs found under `DOCUMENT_ROOT`. The web interface (ADR-0012) only handled the end
of the story, the decision. A person should be able to hand over a PDF in the browser and
watch it move through the pipeline: queue, reading, checks, explanation, entry, ready for a
decision. Two things make this more than a form: the upload is a file written to disk by a
request that anyone with a login can make, and the pipeline takes seconds to minutes, so the
page must follow a process it does not run.

## Decision

**The web process stores and queues; the worker processes.** `POST /uploads` checks the file,
saves it under `uploads/<uuid>.pdf` in the document store and inserts a `process` job (the
same job the `enqueue` command makes, with the stored name as payload and as idempotency key).
The worker is unchanged. The page redirects to `/uploads/<job id>`.

**What is checked, and where.**
- A cap on the request size sits *in front of* the multipart parser (`api/limits.py`), because
  Starlette writes an uploaded file to a temporary file as it arrives with no limit of its
  own. It reads `Content-Length`, and counts the bytes of requests that do not declare one.
  The limit is `MAX_DOCUMENT_BYTES`, the one the ingestion applies, plus the envelope.
- The file must start with `%PDF-`. The file name and the content type the client sent are
  never used: the name is generated, the store creates the file exclusively, and the upload
  directory is resolved under the store root like any other name.
- The same CSRF mechanism as for decisions, bound to the approver and to a fixed scope (the
  nil UUID) because the form is not about one invoice. A decision token does not open the
  upload and the other way round.
- The pipeline's own hardening (parser in a child process, page and size limits, the document
  is data) is unchanged: the upload only adds a way to put a file in front of it.

**Progress is read, not guessed.** The run id is the job id (ADR-0005), and every node writes
an audit event when it finishes (ADR-0009). A pure function (`agent/graph/progress.py`) turns
the job's status and its run's events into the stages: a stage is done when its event, or a
later stage's, exists; the next one is active; a dead job fails the stage it was in and shows
its reason; the explain stage exists only when the checks found anomalies. A later event
proves the earlier stages, so a deployment without the explain node still looks right.

**The browser polls.** The page loads a fragment every second with htmx (the vendored file of
step 09b) and the fragment stops asking for itself once the run is finished, by not carrying
the `hx-` attributes any more. Without JavaScript a `<noscript>` meta refresh does the same
every three seconds. The animation is CSS only and is turned off for people who asked for
reduced motion. A job queued for more than 15 seconds says that a worker may not be running.

## Alternatives considered

- **Server-sent events or a WebSocket** for push. Rejected: a held connection per viewer, a
  proxy and timeout story, and a second mechanism, to save a one-second delay on a process
  that takes longer. Polling a cheap indexed query is simple, stateless and works without
  JavaScript.
- **A timer-driven animation** that moves through the stages whatever happens. Rejected: it
  would show success while the run failed, which is worse than no animation.
- **Processing the upload inside the web request.** Rejected: it would run the PDF parser
  and possibly the model in the web process, outside the worker's timeouts, retries and
  dead-job handling.
- **Keeping the uploaded name.** Rejected: nothing good comes of letting a client choose a
  path, and two uploads of the same name would collide.
- **Accepting a PDF by its extension or content type.** Rejected: both are chosen by the sender.

## Consequences

- A worker must run next to the web server and read the same `DOCUMENT_ROOT`. On AWS the
  store becomes S3 behind the same `save`/`read` methods (the `UploadStore` protocol) and the
  queue SQS; the page does not change.
- `python -m invoice_agent.worker enqueue` also finds files under `uploads/`; because the job
  key is `process:<stored name>`, an uploaded file is not queued twice.
- A rejected or failed upload leaves its file on disk when the failure comes after saving
  (the job died). There is no cleanup job yet.
- Any approver can follow any job by its id, and can submit. There are no roles; this is the
  same trust level as deciding, and is a limit to revisit with more than one team.
- Uploading the same invoice twice is not blocked here: the checks flag it as a duplicate
  (`DUPLICATE_INVOICE`) and a person decides.
- No rate limit on uploads (as for decisions): to be done by the reverse proxy.
