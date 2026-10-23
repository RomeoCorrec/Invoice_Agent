"""Configuration read from environment variables (``.env`` locally, Secrets Manager on AWS)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

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
class Settings:
    database_url: str
    iban_hash_key: bytes
    limits: IngestionLimits


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
    )
