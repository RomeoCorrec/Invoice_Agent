import pytest

from invoice_agent.db.reference_data import demo_contact_address


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("Papeterie Mercier SARL", "compta@papeterie-mercier-sarl.example"),
        ("Loca'Pro Matériel SAS", "compta@loca-pro-materiel-sas.example"),
        ("Café des Ateliers SARL", "compta@cafe-des-ateliers-sarl.example"),
        ("Imprimerie Vasseur & Fils", "compta@imprimerie-vasseur-fils.example"),
    ],
)
def test_the_demo_address_is_derived_from_the_name_on_a_domain_nobody_can_register(
    name: str, address: str
) -> None:
    assert demo_contact_address(name) == address
    assert address.endswith(".example")  # reserved for documentation: it can never be delivered
