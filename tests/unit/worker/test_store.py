from pathlib import Path

import pytest

from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.worker.store import LocalDocumentStore


@pytest.fixture
def store(tmp_path: Path) -> LocalDocumentStore:
    root = tmp_path / "documents"
    (root / "2026-10").mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "2026-10" / "b.pdf").write_bytes(b"%PDF-b")
    (tmp_path / "secret.txt").write_bytes(b"TOP-SECRET")
    return LocalDocumentStore(root)


def test_reads_a_document_by_relative_name(store: LocalDocumentStore) -> None:
    assert store.read("a.pdf", 1000) == b"%PDF-a"
    assert store.read("2026-10/b.pdf", 1000) == b"%PDF-b"


def test_missing_document(store: LocalDocumentStore) -> None:
    with pytest.raises(ExtractionError, match="not found"):
        store.read("nope.pdf", 1000)


def test_a_document_larger_than_the_limit_is_not_read(store: LocalDocumentStore) -> None:
    with pytest.raises(ExtractionError, match="larger than"):
        store.read("a.pdf", 3)


@pytest.mark.parametrize(
    "name",
    ["../secret.txt", "2026-10/../../secret.txt", "..", ".", "", "a.pdf\x00.txt"],
)
def test_names_that_leave_the_root_are_refused(store: LocalDocumentStore, name: str) -> None:
    with pytest.raises(ExtractionError, match=r"outside the store|invalid"):
        store.read(name, 1000)


def test_absolute_paths_are_refused(store: LocalDocumentStore, tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="outside the store"):
        store.read(str(tmp_path / "secret.txt"), 1000)


def test_a_link_pointing_outside_the_root_is_refused(
    store: LocalDocumentStore, tmp_path: Path
) -> None:
    link = tmp_path / "documents" / "link.pdf"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("symbolic links are not available here")
    with pytest.raises(ExtractionError, match="outside the store"):
        store.read("link.pdf", 1000)


def test_errors_never_contain_file_contents_or_paths(store: LocalDocumentStore) -> None:
    with pytest.raises(ExtractionError) as error:
        store.read("../secret.txt", 1000)
    assert "SECRET" not in str(error.value)
    assert "secret.txt" not in str(error.value)


def test_save_writes_under_uploads_with_a_generated_name_and_reads_back(
    store: LocalDocumentStore, tmp_path: Path
) -> None:
    first, second = store.save(b"%PDF-new"), store.save(b"%PDF-new")
    assert first != second  # never an overwrite, even for the same content
    for name in (first, second):
        assert name.startswith("uploads/") and name.endswith(".pdf")
        assert store.read(name, 1000) == b"%PDF-new"
        assert (tmp_path / "documents" / name).is_file()
    assert (tmp_path / "secret.txt").read_bytes() == b"TOP-SECRET"
