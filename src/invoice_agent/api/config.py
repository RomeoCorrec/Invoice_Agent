"""Configuration of the web application, from the environment."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from invoice_agent.api.auth import MIN_SECRET_LENGTH, Approvers, AuthConfigError
from invoice_agent.config import ConfigError


@dataclass(frozen=True)
class WebConfig:
    approvers: Approvers = field(repr=False)
    secret: bytes = field(repr=False)
    """Signs the CSRF tokens. Changing it invalidates the forms that are open."""

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "WebConfig":
        try:
            approvers = Approvers.parse(environ.get("APPROVERS", ""))
        except AuthConfigError as exc:
            raise ConfigError(str(exc)) from exc
        secret = environ.get("WEB_SECRET", "")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"WEB_SECRET must be set and at least {MIN_SECRET_LENGTH} characters")
        return cls(approvers=approvers, secret=secret.encode())
