import pytest

from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import Party
from invoice_agent.domain.rules.anomaly import AnomalyCode
from invoice_agent.domain.rules.identifiers import check_iban, check_siren, luhn_valid
from tests.factories import IBAN_KEY, make_data, make_supplier


@pytest.mark.parametrize(
    "number",
    ["151757747", "419058185", "586323263", "73282932000074", "356000000"],
)
def test_valid_luhn_numbers(number: str) -> None:
    assert luhn_valid(number)


@pytest.mark.parametrize("number", ["151757748", "123456789", "", "12a456789", "000000001"])
def test_invalid_luhn_numbers(number: str) -> None:
    assert not luhn_valid(number)


def test_every_digit_change_breaks_the_checksum() -> None:
    """Luhn detects any single-digit error, which is what INV-19 relies on."""
    base = "151757747"
    for index in range(len(base)):
        for digit in "0123456789":
            if digit != base[index]:
                altered = base[:index] + digit + base[index + 1 :]
                assert not luhn_valid(altered)


def test_valid_siren_is_not_flagged() -> None:
    assert check_siren(make_data(seller=Party(name="X", siren="151757747")), None) == []


def test_missing_siren_is_not_flagged() -> None:
    assert check_siren(make_data(seller=Party(name="X")), None) == []


def test_invalid_siren_is_flagged_like_inv_19() -> None:
    [anomaly] = check_siren(make_data(seller=Party(name="X", siren="151757748")), None)
    assert anomaly.code is AnomalyCode.INVALID_SIREN
    assert anomaly.evidence == {"siren": "151757748"}


def test_same_iban_is_accepted() -> None:
    iban = IbanFingerprint.from_iban("FR7699053965263585803695442", IBAN_KEY)
    assert check_iban(make_data(iban=iban), make_supplier(iban=iban)) == []


def test_different_iban_is_flagged_like_inv_18() -> None:
    supplier = make_supplier(
        iban=IbanFingerprint.from_iban("FR7699470095021252774786577", IBAN_KEY)
    )
    data = make_data(iban=IbanFingerprint.from_iban("FR7699546325778456691797437", IBAN_KEY))
    [anomaly] = check_iban(data, supplier)
    assert anomaly.code is AnomalyCode.IBAN_MISMATCH
    assert anomaly.evidence == {
        "invoice_iban": "FR76 **** 7437",
        "reference_iban": "FR76 **** 6577",
    }


def test_iban_is_not_checked_when_either_side_is_unknown() -> None:
    iban = IbanFingerprint.from_iban("FR7699053965263585803695442", IBAN_KEY)
    assert check_iban(make_data(iban=iban), None) == []
    assert check_iban(make_data(iban=iban), make_supplier(iban=None)) == []
    assert check_iban(make_data(iban=None), make_supplier(iban=iban)) == []


def test_well_formed_but_foreign_siren_on_a_known_supplier_is_flagged() -> None:
    """Identified by VAT number, but the SIREN is another (valid) company's."""
    supplier = make_supplier(siren="151757747")
    seller = Party(name="Papeterie Mercier SARL", siren="419058185", vat_number=supplier.vat_number)
    [anomaly] = check_siren(make_data(seller=seller), supplier)
    assert anomaly.code is AnomalyCode.INVALID_SIREN
    assert "differs" in anomaly.message


def test_matching_siren_on_a_known_supplier_is_fine() -> None:
    supplier = make_supplier(siren="151757747")
    assert check_siren(make_data(seller=Party(name="X", siren="151757747")), supplier) == []


def test_invalid_siren_on_a_known_supplier_yields_a_single_anomaly_like_inv_19() -> None:
    supplier = make_supplier(siren="151757747")
    anomalies = check_siren(make_data(seller=Party(name="X", siren="151757748")), supplier)
    assert [a.code for a in anomalies] == [AnomalyCode.INVALID_SIREN]
    assert "Luhn" in anomalies[0].message


BANK_NOTICE = (
    "Suite à un changement de banque, merci d'utiliser nos nouvelles coordonnées bancaires."
)


def test_bank_change_notice_is_flagged_even_if_the_iban_is_unchanged() -> None:
    iban = IbanFingerprint.from_iban("FR7699053965263585803695442", IBAN_KEY)
    [anomaly] = check_iban(make_data(iban=iban, notes=(BANK_NOTICE,)), make_supplier(iban=iban))
    assert anomaly.code is AnomalyCode.IBAN_MISMATCH
    assert "new bank details" in anomaly.message


def test_bank_change_notice_is_flagged_when_no_iban_is_on_file() -> None:
    assert check_iban(make_data(notes=(BANK_NOTICE,)), make_supplier(iban=None))
    assert check_iban(make_data(notes=(BANK_NOTICE,)), None)


def test_iban_written_in_a_note_is_flagged() -> None:
    notes = ("Merci de régler sur FR76 9905 3965 2635 8580 3695 442 uniquement.",)
    [anomaly] = check_iban(make_data(notes=notes), None)
    assert "IBAN is written" in anomaly.message


def test_different_iban_and_notice_make_a_single_anomaly_like_inv_18() -> None:
    supplier = make_supplier(
        iban=IbanFingerprint.from_iban("FR7699470095021252774786577", IBAN_KEY)
    )
    data = make_data(
        iban=IbanFingerprint.from_iban("FR7699546325778456691797437", IBAN_KEY),
        notes=(BANK_NOTICE,),
    )
    anomalies = check_iban(data, supplier)
    assert [a.code for a in anomalies] == [AnomalyCode.IBAN_MISMATCH]
    assert "differs" in anomalies[0].message
    assert "new bank details" in anomalies[0].message


@pytest.mark.parametrize(
    "note",
    ["Paiement à 30 jours fin de mois.", "Merci de votre confiance.", "Réf. client 2026-0042"],
)
def test_ordinary_notes_do_not_touch_the_iban_rule(note: str) -> None:
    assert check_iban(make_data(notes=(note,)), make_supplier(iban=None)) == []
