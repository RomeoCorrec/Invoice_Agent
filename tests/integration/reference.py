"""The dataset's reference data (suppliers, purchase orders) loaded into the database."""

import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from invoice_agent.db.repositories import PurchaseOrderRepository, SupplierRepository
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.purchase_order import PurchaseOrder, PurchaseOrderLine
from invoice_agent.domain.supplier import Supplier
from tests.factories import IBAN_KEY

DATASET = Path(__file__).parents[1] / "fixtures" / "invoice-test-set"


def load_reference(session: Session) -> dict[str, UUID]:
    """Insert suppliers and purchase orders; return supplier id by code."""
    reference = DATASET / "reference"
    supplier_ids: dict[str, UUID] = {}
    suppliers = json.loads((reference / "suppliers.json").read_text(encoding="utf-8"))
    for raw in suppliers:
        supplier = Supplier(
            id=uuid4(),
            code=raw["code"],
            name=raw["name"],
            siren=raw["siren"],
            siret=raw["siret"],
            vat_number=raw["vat_number"],
            iban=IbanFingerprint.from_iban(raw["iban"], IBAN_KEY),
            default_expense_account=raw["default_expense_account"],
            supplier_account=raw["supplier_account"],
        )
        SupplierRepository(session).add(supplier)
        supplier_ids[raw["code"]] = supplier.id
    orders = json.loads((reference / "purchase_orders.json").read_text(encoding="utf-8"))
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
