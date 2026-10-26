"""Load reference data (suppliers and purchase orders) from JSON files into the database.

The files are the dataset's ``reference/suppliers.json`` and ``purchase_orders.json``. This is
a development and demo convenience: in production the suppliers and orders come from the
accounting system.
"""

import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from invoice_agent.db.repositories import PurchaseOrderRepository, SupplierRepository
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.supplier import Supplier


def load_reference_data(session: Session, directory: Path, iban_key: bytes) -> dict[str, UUID]:
    """Insert the suppliers and purchase orders; return the supplier ids by code."""
    supplier_ids: dict[str, UUID] = {}
    suppliers = json.loads((directory / "suppliers.json").read_text(encoding="utf-8"))
    for raw in suppliers:
        supplier = Supplier(
            id=uuid4(),
            code=raw["code"],
            name=raw["name"],
            siren=raw["siren"],
            siret=raw["siret"],
            vat_number=raw["vat_number"],
            iban=IbanFingerprint.from_iban(raw["iban"], iban_key),
            default_expense_account=raw["default_expense_account"],
            supplier_account=raw["supplier_account"],
        )
        SupplierRepository(session).add(supplier)
        supplier_ids[raw["code"]] = supplier.id
    orders = json.loads((directory / "purchase_orders.json").read_text(encoding="utf-8"))
    for raw in orders:
        PurchaseOrderRepository(session).add(
            PurchaseOrder(
                id=uuid4(),
                number=raw["number"],
                supplier_id=supplier_ids[raw["supplier_code"]],
                order_date=raw["order_date"],
                status=raw["status"],
                lines=tuple(PurchaseOrderLine(**line) for line in raw["lines"]),
            )
        )
    return supplier_ids
