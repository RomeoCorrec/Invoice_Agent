"""IBAN handling without keeping the IBAN itself.

Only a masked form (for display) and a keyed hash (for equality checks) are stored,
so a leaked database or log does not expose bank details. The key comes from the
environment and is passed in: this module does no I/O.
"""

import hashlib
import hmac
import re

from pydantic import Field

from invoice_agent.domain.base import DomainModel

_IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
IBAN_IN_TEXT = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
"""Matches something that looks like an IBAN inside free text (used to keep them out of logs)."""


def redact_ibans(text: str) -> str:
    """Replace anything that looks like an IBAN, so free text can be logged safely."""
    return IBAN_IN_TEXT.sub("[IBAN redacted]", text)


def normalize_iban(raw: str) -> str:
    iban = "".join(raw.split()).upper()
    if not _IBAN_SHAPE.match(iban):
        raise ValueError("not a valid IBAN shape")
    return iban


def mask_iban(raw: str) -> str:
    """``FR7699053965263585803695442`` -> ``FR76 **** 5442``."""
    iban = normalize_iban(raw)
    return f"{iban[:4]} **** {iban[-4:]}"


def hash_iban(raw: str, key: bytes) -> str:
    """Keyed hash (HMAC-SHA256, hex) of the normalised IBAN."""
    if not key:
        raise ValueError("a non-empty hashing key is required")
    return hmac.new(key, normalize_iban(raw).encode(), hashlib.sha256).hexdigest()


class IbanFingerprint(DomainModel):
    masked: str = Field(pattern=r"^[A-Z]{2}\d{2} \*{4} [A-Z0-9]{4}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_iban(cls, raw: str, key: bytes) -> "IbanFingerprint":
        return cls(masked=mask_iban(raw), digest=hash_iban(raw, key))

    def matches(self, other: "IbanFingerprint") -> bool:
        return hmac.compare_digest(self.digest, other.digest)
