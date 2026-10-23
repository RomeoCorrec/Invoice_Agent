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
