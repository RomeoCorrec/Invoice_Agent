# Third-party files kept in the repository

Python dependencies are declared in `pyproject.toml` and locked in `uv.lock`. The files below
are not Python packages, so they are kept in the repository and pinned by their hash; a test
fails if one of them changes without this page being updated.

| File | What | Version | Licence | SHA-256 |
|---|---|---|---|---|
| `src/invoice_agent/api/static/htmx.min.js` | [htmx](https://htmx.org), HTML-over-the-wire interactions | 2.0.4 | Zero-Clause BSD | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |

It is served by the application itself, never from a CDN, so the pages make no external
request and the Content-Security-Policy can stay `script-src 'self'`. Its configuration is in
the page `<meta name="htmx-config">`: `eval`, inline indicator styles, script tags in responses
and requests to other origins are all turned off, which the policy would block anyway.

To update it: download the new file from
`https://cdn.jsdelivr.net/npm/htmx.org@<version>/dist/htmx.min.js`, check it against the
release, replace the file, and change the version and hash here and in
`tests/unit/api/test_vendored_files.py`.
