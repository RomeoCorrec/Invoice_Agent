import hashlib
from pathlib import Path

HTMX = Path(__file__).parents[3] / "src" / "invoice_agent" / "api" / "static" / "htmx.min.js"
HTMX_SHA256 = "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"
THIRD_PARTY = Path(__file__).parents[3] / "docs" / "third-party.md"


def test_htmx_is_the_pinned_file() -> None:
    assert hashlib.sha256(HTMX.read_bytes()).hexdigest() == HTMX_SHA256


def test_the_page_that_documents_the_vendored_files_matches_the_pin() -> None:
    page = THIRD_PARTY.read_text(encoding="utf-8")
    assert HTMX_SHA256 in page and "htmx.min.js" in page and "2.0.4" in page
