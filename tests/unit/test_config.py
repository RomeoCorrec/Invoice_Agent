import pytest

from invoice_agent.config import ConfigError, load_settings

VALID = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db", "IBAN_HASH_KEY": "k" * 32}


def test_defaults() -> None:
    settings = load_settings(VALID)
    assert settings.database_url == VALID["DATABASE_URL"]
    assert settings.iban_hash_key == b"k" * 32
    assert settings.limits.max_pages == 20
    assert settings.limits.max_document_bytes == 10 * 1024 * 1024


def test_limits_can_be_overridden() -> None:
    settings = load_settings({**VALID, "MAX_PDF_PAGES": "5", "MAX_DOCUMENT_BYTES": "1000"})
    assert settings.limits.max_pages == 5
    assert settings.limits.max_document_bytes == 1000


@pytest.mark.parametrize("missing", ["DATABASE_URL", "IBAN_HASH_KEY"])
def test_missing_required_variable(missing: str) -> None:
    env = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


def test_short_hash_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as error:
        load_settings({**VALID, "IBAN_HASH_KEY": "secret"})
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("bad", ["abc", "0", "-3", ""])
def test_limits_must_be_positive_integers(bad: str) -> None:
    with pytest.raises(ConfigError, match="MAX_PDF_PAGES"):
        load_settings({**VALID, "MAX_PDF_PAGES": bad})


def test_worker_defaults() -> None:
    worker = load_settings(VALID).worker
    assert (worker.poll_interval_s, worker.job_timeout_s, worker.max_attempts) == (2.0, 60, 5)
    assert (worker.backoff_base_s, worker.backoff_cap_s) == (5, 600)
    assert worker.document_root is None
    assert worker.lease_s == 120  # twice the timeout: a live worker never loses its lease


def test_worker_settings_can_be_overridden() -> None:
    env = {
        **VALID,
        "WORKER_POLL_SECONDS": "0.5",
        "JOB_TIMEOUT_SECONDS": "10",
        "JOB_MAX_ATTEMPTS": "3",
        "DOCUMENT_ROOT": "/data/invoices",
    }
    worker = load_settings(env).worker
    assert worker.poll_interval_s == 0.5
    assert worker.lease_s == 20
    assert worker.max_attempts == 3
    assert str(worker.document_root).replace("\\", "/") == "/data/invoices"


@pytest.mark.parametrize(
    ("name", "bad"),
    [("WORKER_POLL_SECONDS", "0"), ("WORKER_POLL_SECONDS", "soon"), ("JOB_TIMEOUT_SECONDS", "-1")],
)
def test_invalid_worker_settings_are_refused(name: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**VALID, name: bad})
