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


def test_llm_defaults_and_overrides() -> None:
    assert load_settings(VALID).llm.api_key is None
    assert load_settings(VALID).llm.model == "claude-haiku-5-5"
    env = {
        **VALID,
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "LLM_MODEL": "claude-sonnet-5-5",
        "LLM_TIMEOUT_SECONDS": "30",
        "LLM_MAX_RETRIES": "1",
        "LLM_EFFORT": "medium",
    }
    llm = load_settings(env).llm
    assert (llm.api_key, llm.model, llm.timeout_s, llm.max_retries, llm.effort) == (
        "sk-ant-test",
        "claude-sonnet-5-5",
        30.0,
        1,
        "medium",
    )


def test_an_empty_api_key_means_no_language_model() -> None:
    assert load_settings({**VALID, "ANTHROPIC_API_KEY": ""}).llm.api_key is None


def test_invalid_effort_is_refused() -> None:
    with pytest.raises(ConfigError, match="LLM_EFFORT"):
        load_settings({**VALID, "LLM_EFFORT": "extreme"})


def test_secrets_never_appear_in_the_repr_of_the_settings() -> None:
    env = {**VALID, "ANTHROPIC_API_KEY": "sk-ant-very-secret", "IBAN_HASH_KEY": "h" * 40}
    shown = repr(load_settings(env))
    assert "sk-ant-very-secret" not in shown
    assert "hhhh" not in shown
    assert "postgresql" not in shown


def test_mail_defaults_and_overrides() -> None:
    settings = load_settings(VALID)
    assert settings.mailroom_database_url is None  # the agent's own database
    assert (settings.mail.reply_deadline_days, settings.mail.poll_interval_s) == (3, 60)
    assert settings.mail.lookback_days == 7
    settings = load_settings(
        {
            **VALID,
            "MAILROOM_DATABASE_URL": "postgresql+psycopg://u:p@other/db",
            "REPLY_DEADLINE_DAYS": "5",
            "MAIL_POLL_SECONDS": "30",
            "MAIL_LOOKBACK_DAYS": "14",
        }
    )
    assert settings.mailroom_database_url == "postgresql+psycopg://u:p@other/db"
    assert (settings.mail.reply_deadline_days, settings.mail.poll_interval_s) == (5, 30)
    assert settings.mail.lookback_days == 14


@pytest.mark.parametrize("name", ["REPLY_DEADLINE_DAYS", "MAIL_POLL_SECONDS", "MAIL_LOOKBACK_DAYS"])
@pytest.mark.parametrize("bad", ["0", "-1", "soon"])
def test_invalid_mail_settings_are_refused(name: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**VALID, name: bad})


def test_the_mailroom_url_does_not_appear_in_the_repr() -> None:
    settings = load_settings({**VALID, "MAILROOM_DATABASE_URL": "postgresql://u:secret@h/db"})
    assert "secret" not in repr(settings)
