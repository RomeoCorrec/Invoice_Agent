"""Supplier resolution from the seller written on the invoice."""

from collections.abc import Sequence

from invoice_agent.domain.invoice import Party
from invoice_agent.domain.supplier import Supplier


def resolve_supplier(seller: Party, candidates: Sequence[Supplier]) -> Supplier | None:
    """Match by SIREN first, then by VAT number.

    The VAT fallback matters when the SIREN is mistyped or altered (INV-19): the supplier
    is still recognised and the bad SIREN is reported on its own, instead of being
    reported as an unknown supplier. The name is deliberately not used: it is free text.
    """
    if seller.siren:
        for supplier in candidates:
            if supplier.siren == seller.siren:
                return supplier
    if seller.vat_number:
        for supplier in candidates:
            if supplier.vat_number == seller.vat_number:
                return supplier
    return None
