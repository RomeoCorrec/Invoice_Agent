import pytest
from pydantic import ValidationError

from invoice_agent.domain.iban import (
    IBAN_IN_TEXT,
    IbanFingerprint,
    hash_iban,
    mask_iban,
    redact_ibans,
)

KEY = b"test-key"
IBAN = "FR7699053965263585803695442"


def test_mask_keeps_prefix_and_last_four() -> None:
    assert mask_iban(IBAN) == "FR76 **** 5442"


def test_mask_ignores_spaces_and_case() -> None:
    assert mask_iban("fr76 9905 3965 2635 8580 3695 442") == "FR76 **** 5442"


def test_hash_is_stable_and_format_independent() -> None:
    assert hash_iban(IBAN, KEY) == hash_iban("FR76 9905 3965 2635 8580 3695 442", KEY)


def test_hash_depends_on_key_and_iban() -> None:
    assert hash_iban(IBAN, KEY) != hash_iban(IBAN, b"other-key")
    assert hash_iban(IBAN, KEY) != hash_iban("FR7699061508882043304951471", KEY)


def test_empty_key_is_refused() -> None:
    with pytest.raises(ValueError, match="key"):
        hash_iban(IBAN, b"")


@pytest.mark.parametrize("bad", ["", "12345", "FR76", "not an iban"])
def test_malformed_iban_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="IBAN"):
        mask_iban(bad)


def test_fingerprint_matching() -> None:
    a = IbanFingerprint.from_iban(IBAN, KEY)
    assert a.matches(IbanFingerprint.from_iban(IBAN.lower(), KEY))
    assert not a.matches(IbanFingerprint.from_iban("FR7699061508882043304951471", KEY))


def test_fingerprint_does_not_contain_the_iban() -> None:
    dumped = IbanFingerprint.from_iban(IBAN, KEY).model_dump_json()
    assert IBAN not in dumped
    assert not IBAN_IN_TEXT.search(dumped)


def test_fingerprint_rejects_a_raw_iban_as_masked_value() -> None:
    with pytest.raises(ValidationError):
        IbanFingerprint(masked=IBAN, digest="0" * 64)


def test_iban_in_text_detection() -> None:
    assert IBAN_IN_TEXT.search(f"pay to {IBAN} please")
    assert IBAN_IN_TEXT.search("pay to FR76 9905 3965 2635 8580 3695 442")
    assert not IBAN_IN_TEXT.search("invoice INV-2026-0001 total 12.50")


def test_redact_ibans_in_free_text() -> None:
    text = f"Pay {IBAN} or FR76 9905 3965 2635 8580 3695 442 today, ref INV-2026-0001"
    redacted = redact_ibans(text)
    assert "[IBAN redacted]" in redacted
    assert not IBAN_IN_TEXT.search(redacted)
    assert "INV-2026-0001" in redacted
