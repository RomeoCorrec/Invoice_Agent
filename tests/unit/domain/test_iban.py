import pytest
from pydantic import ValidationError

from invoice_agent.domain.iban import (
    IbanFingerprint,
    contains_iban,
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
    assert not contains_iban(dumped)


def test_fingerprint_rejects_a_raw_iban_as_masked_value() -> None:
    with pytest.raises(ValidationError):
        IbanFingerprint(masked=IBAN, digest="0" * 64)


def test_iban_in_text_detection() -> None:
    assert contains_iban(f"pay to {IBAN} please")
    assert contains_iban("pay to FR76 9905 3965 2635 8580 3695 442")
    assert not contains_iban("invoice INV-2026-0001 total 12.50")


def test_redact_ibans_in_free_text() -> None:
    text = f"Pay {IBAN} or FR76 9905 3965 2635 8580 3695 442 today, ref INV-2026-0001"
    redacted = redact_ibans(text)
    assert "[IBAN redacted]" in redacted
    assert not contains_iban(redacted)
    assert "INV-2026-0001" in redacted


@pytest.mark.parametrize(
    "disguised",
    [
        "FR76-9905-3965-2635-8580-3695-442",
        "FR76.9905.3965.2635.8580.3695.442",
        "FR76_9905_3965_2635_8580_3695_442",
        "fr76 9905 3965 2635 8580 3695 442",
        "FR76\u200b9905\u200b3965\u200b2635\u200b8580\u200b3695\u200b442",  # zero-width spaces
        "FR76\u00ad99053965263585803695442",  # soft hyphen
        "\uff26\uff32\uff17\uff16 9905 3965 2635 8580 3695 442",  # full-width letters and digits
        "FR76 9905 3965\n2635 8580\t3695 442",
    ],
)
def test_disguised_ibans_are_still_found_and_redacted(disguised: str) -> None:
    assert contains_iban(f"please pay {disguised} thanks")
    assert "[IBAN redacted]" in redact_ibans(f"please pay {disguised} thanks")


def test_redaction_keeps_the_case_of_the_rest_of_the_text() -> None:
    assert redact_ibans(f"Seller Café {IBAN} done") == "Seller Café [IBAN redacted] done"


def test_hex_digest_and_references_are_not_taken_for_ibans() -> None:
    digest = IbanFingerprint.from_iban(IBAN, KEY).digest
    assert not contains_iban(digest)
    assert not contains_iban("Ref. CT-26-1188 du 12/10/2026, commande PO-2026-0105")


def test_an_iban_dressed_as_a_uuid_is_still_found() -> None:
    """The detector must stay strict: identifiers are exempted by where they come from, not
    by what they look like."""
    from tests.factories import uuid_shaped_iban

    hidden = uuid_shaped_iban()
    assert contains_iban(hidden)
    assert "[IBAN redacted]" in redact_ibans(hidden)
