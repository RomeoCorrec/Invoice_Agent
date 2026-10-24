"""Where the documents to ingest live.

A job carries a *name* (a relative path or a key), never the document. The store resolves
it, and refuses anything that would leave its root. A local directory for now; an S3-backed
store will implement the same protocol on AWS.
"""

from pathlib import Path
from typing import Protocol

from invoice_agent.ingestion.document import ExtractionError


class DocumentStore(Protocol):
    def read(self, name: str, max_bytes: int) -> bytes:
        """The bytes of document ``name``; ExtractionError if it is missing or too large."""
        ...


class LocalDocumentStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def read(self, name: str, max_bytes: int) -> bytes:
        path = self._resolve(name)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ExtractionError("document not found in the store") from exc
        if size > max_bytes:
            raise ExtractionError(f"document is larger than {max_bytes} bytes")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ExtractionError("document could not be read from the store") from exc

    def _resolve(self, name: str) -> Path:
        """Join under the root and refuse ``..``, absolute paths and links leaving the root."""
        if not name or "\x00" in name:
            raise ExtractionError("invalid document name")
        candidate = (self._root / name).resolve()
        if not candidate.is_relative_to(self._root) or candidate == self._root:
            raise ExtractionError("document name is outside the store")
        return candidate
