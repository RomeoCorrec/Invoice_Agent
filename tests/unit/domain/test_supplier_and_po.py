from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.supplier import Supplier


def make_supplier(**overrides: object) -> Supplier:
    data: dict[str, object] = {
        "id": uuid4(),
        "code": "SUP-001",
        "name": "Papeterie Mercier SARL",
        "siren": "151757747",
        "siret": "15175774726270",
        "iban": IbanFingerprint.from_iban("FR7699053965263585803695442", b"k"),
        "default_expense_account": "6064",
        "supplier_account": "401MERC",
    }
    return Supplier(**{**data, **overrides})  # type: ignore[arg-type]


def test_supplier_is_immutable() -> None:
    with pytest.raises(ValidationError):
        make_supplier().name = "Other"  # type: ignore[misc]


def test_supplier_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        make_supplier(iban_plain="FR7699053965263585803695442")


def test_supplier_rejects_malformed_siren() -> None:
    with pytest.raises(ValidationError):
        make_supplier(siren="123")


def test_purchase_order_total_is_the_sum_of_its_lines() -> None:
    lines = (
        PurchaseOrderLine(
            description="Ramettes", quantity="10", unit_price="24.90", amount_ht="249.00"
        ),
        PurchaseOrderLine(description="Stylos", quantity="6", unit_price="8.50", amount_ht="51.00"),
    )
    order = PurchaseOrder(
        id=uuid4(),
        number="PO-2026-0101",
        supplier_id=uuid4(),
        order_date=date(2026, 9, 1),
        lines=lines,
    )
    assert order.total_ht == Decimal("300.00")
    assert order.model_dump()["total_ht"] == Decimal("300.00")


def test_purchase_order_needs_at_least_one_line() -> None:
    with pytest.raises(ValidationError):
        PurchaseOrder(
            id=uuid4(), number="PO-1", supplier_id=uuid4(), order_date=date(2026, 9, 1), lines=()
        )


def test_a_supplier_may_have_no_contact_address() -> None:
    assert make_supplier().contact_email is None
    assert make_supplier(contact_email="compta@papeterie-mercier.example").contact_email == (
        "compta@papeterie-mercier.example"
    )


@pytest.mark.parametrize(
    "address",
    [
        "no-at-sign",
        "two@@signs.example",
        "name@",
        "@domain.example",
        "name@domain",  # no dot
        "a b@domain.example",  # a space
        "name@domain.example\nBcc: attacker@evil.example",  # a header smuggled in
        "one@domain.example, two@domain.example",  # several recipients
        "name@domain.example>",
        "x" * 250 + "@d.example",  # longer than an address can be
    ],
)
def test_a_contact_address_must_look_like_one_address(address: str) -> None:
    with pytest.raises(ValidationError):
        make_supplier(contact_email=address)
