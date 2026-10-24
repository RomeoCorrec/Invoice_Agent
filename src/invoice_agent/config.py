"""Configuration read from environment variables (``.env`` locally, Secrets Manager on AWS)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MIN_IBAN_KEY_LENGTH = 16


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestionLimits:
    max_document_bytes: int = 10 * 1024 * 1024
    max_pages: int = 20
    max_xml_bytes: int = 2 * 1024 * 1024
    max_text_chars: int = 100_000


@dataclass(frozen=True)
class WorkerSettings:
    poll_interval_s: float = 2.0
    job_timeout_s: int = 60
    max_attempts: int = 5
    backoff_base_s: int = 5
    backoff_cap_s: int = 600
    document_root: Path | None = None
    """Directory holding the documents to ingest; required to run the worker."""

    @property
    def lease_s(self) -> int:
        """A job's lease: long enough to outlive its timeout, so a live worker never loses it."""
        return 2 * self.job_timeout_s


@dataclass(frozen=True)
class Settings:
    database_url: str
    iban_hash_key: bytes
    limits: IngestionLimits
    worker: WorkerSettings = field(default_factory=WorkerSettings)


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build the settings, failing loudly (without echoing secrets) when something is missing."""
    env = os.environ if environ is None else environ
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise ConfigError("DATABASE_URL is not set")
    key = env.get("IBAN_HASH_KEY", "")
    if len(key) < MIN_IBAN_KEY_LENGTH:
        raise ConfigError(
            f"IBAN_HASH_KEY must be set and at least {MIN_IBAN_KEY_LENGTH} characters"
        )
    defaults = IngestionLimits()
    worker_defaults = WorkerSettings()
    root = env.get("DOCUMENT_ROOT")
    return Settings(
        database_url=database_url,
        iban_hash_key=key.encode(),
        limits=IngestionLimits(
            max_document_bytes=_positive_int(
                env, "MAX_DOCUMENT_BYTES", defaults.max_document_bytes
            ),
            max_pages=_positive_int(env, "MAX_PDF_PAGES", defaults.max_pages),
            max_xml_bytes=_positive_int(env, "MAX_XML_BYTES", defaults.max_xml_bytes),
            max_text_chars=_positive_int(env, "MAX_TEXT_CHARS", defaults.max_text_chars),
        ),
        worker=WorkerSettings(
            poll_interval_s=_positive_float(
                env, "WORKER_POLL_SECONDS", worker_defaults.poll_interval_s
            ),
            job_timeout_s=_positive_int(env, "JOB_TIMEOUT_SECONDS", worker_defaults.job_timeout_s),
            max_attempts=_positive_int(env, "JOB_MAX_ATTEMPTS", worker_defaults.max_attempts),
            backoff_base_s=_positive_int(
                env, "JOB_BACKOFF_BASE_SECONDS", worker_defaults.backoff_base_s
            ),
            backoff_cap_s=_positive_int(
                env, "JOB_BACKOFF_CAP_SECONDS", worker_defaults.backoff_cap_s
            ),
            document_root=Path(root) if root else None,
        ),
    )
