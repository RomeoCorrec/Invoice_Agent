"""IBAN handling without keeping the IBAN itself.

Only a masked form (for display) and a keyed hash (for equality checks) are stored,
so a leaked database or log does not expose bank details. The key comes from the
environment and is passed in: this module does no I/O.
"""

import hashlib
import hmac
import re
import unicodedata

from pydantic import Field

from invoice_agent.domain.base import DomainModel

_IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
# Total length of an IBAN by country; other countries are tried from 15 to 34 characters.
_COUNTRY_LENGTHS = {
    "FR": 27, "MC": 27, "DE": 22, "BE": 16, "ES": 24, "IT": 27, "NL": 18, "GB": 22,
    "CH": 21, "LU": 20, "PT": 25, "IE": 22, "AT": 20,
}  # fmt: skip
_MIN_LENGTH, _MAX_LENGTH = 15, 34
_START = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}", re.IGNORECASE)
_SEPARATORS = "-._"


def fold_text(text: str) -> str:
    """Compatibility-normalise and drop invisible format characters (zero-width, soft hyphen).

    Closes the easy bypasses of text matching: full-width digits, zero-width spaces.
    """
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(char for char in normalized if unicodedata.category(char) != "Cf")


def _mod97_ok(iban: str) -> bool:
    """ISO 13616 check: move the first four characters to the end, letters become 10..35."""
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def _iban_spans(folded: str) -> list[tuple[int, int]]:
    """Spans of valid IBANs in ``folded`` text, whatever separates their characters."""
    spans: list[tuple[int, int]] = []
    resume = 0
    for start in _START.finditer(folded):
        if start.start() < resume:
            continue
        chars: list[str] = []
        ends: list[int] = []
        for index in range(start.start(), len(folded)):
            char = folded[index]
            if char.isascii() and char.isalnum():
                chars.append(char.upper())
                ends.append(index + 1)
                if len(chars) == _MAX_LENGTH:
                    break
            elif not (char.isspace() or char in _SEPARATORS):
                break
        known = _COUNTRY_LENGTHS.get("".join(chars[:2]))
        lengths = [known] if known else range(_MAX_LENGTH, _MIN_LENGTH - 1, -1)
        for length in lengths:
            if length <= len(chars) and _mod97_ok("".join(chars[:length])):
                spans.append((start.start(), ends[length - 1]))
                resume = ends[length - 1]
                break
    return spans


def contains_iban(text: str) -> bool:
    """True if ``text`` holds a valid IBAN, however it is spaced out or disguised."""
    return bool(_iban_spans(fold_text(text)))


def redact_ibans(text: str) -> str:
    """Replace every IBAN found in ``text``, so free text can be logged safely.

    The text is returned in its folded form (see ``fold_text``).
    """
    folded = fold_text(text)
    pieces: list[str] = []
    cursor = 0
    for start, end in _iban_spans(folded):
        pieces += [folded[cursor:start], "[IBAN redacted]"]
        cursor = end
    pieces.append(folded[cursor:])
    return "".join(pieces)


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
