"""The dataset's suppliers and purchase orders as an in-memory validation context.

A single-document evaluation has no database and no history. The context built here has the
reference data only, so the rules can say what they find in one document; whatever depends on
earlier invoices (duplicates, an order already billed) is left out of the comparison.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from evals.dataset import DATASET_DIR
from invoice_agent.agent.checks import ValidationContext
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from invoice_agent.domain.supplier import Supplier
from invoice_agent.matching.supplier import resolve_supplier


@dataclass(frozen=True)
class Reference:
    suppliers: list[Supplier]
    orders: list[PurchaseOrder]
    supplier_codes: dict[UUID, str]
    order_numbers: dict[UUID, str]

    def context_for(self, data: InvoiceData) -> ValidationContext:
        candidates = tuple(
            supplier
            for supplier in self.suppliers
            if (data.seller.siren and supplier.siren == data.seller.siren)
            or (data.seller.vat_number and supplier.vat_number == data.seller.vat_number)
        )
        supplier = resolve_supplier(data.seller, candidates)
        referenced = next(
            (order for order in self.orders if order.number == data.purchase_order_ref), None
        )
        open_orders = tuple(
            order
            for order in self.orders
            if supplier is not None
            and order.supplier_id == supplier.id
            and order.status is PurchaseOrderStatus.OPEN
        )
        return ValidationContext(
            supplier_candidates=candidates, referenced_order=referenced, open_orders=open_orders
        )


def load_reference(iban_key: bytes, dataset_dir: Path = DATASET_DIR) -> Reference:
    reference = dataset_dir / "reference"
    suppliers: list[Supplier] = []
    codes: dict[UUID, str] = {}
    for raw in json.loads((reference / "suppliers.json").read_text(encoding="utf-8")):
        supplier = Supplier(
            id=uuid5(NAMESPACE_URL, f"supplier:{raw['code']}"),
            code=raw["code"],
            name=raw["name"],
            siren=raw["siren"],
            siret=raw["siret"],
            vat_number=raw["vat_number"],
            iban=IbanFingerprint.from_iban(raw["iban"], iban_key),
            default_expense_account=raw["default_expense_account"],
            supplier_account=raw["supplier_account"],
        )
        suppliers.append(supplier)
        codes[supplier.id] = supplier.code
    orders: list[PurchaseOrder] = []
    numbers: dict[UUID, str] = {}
    for raw in json.loads((reference / "purchase_orders.json").read_text(encoding="utf-8")):
        order = PurchaseOrder(
            id=uuid5(NAMESPACE_URL, f"order:{raw['number']}"),
            number=raw["number"],
            supplier_id=uuid5(NAMESPACE_URL, f"supplier:{raw['supplier_code']}"),
            order_date=raw["order_date"],
            status=raw["status"],
            lines=tuple(PurchaseOrderLine(**line) for line in raw["lines"]),
        )
        orders.append(order)
        numbers[order.id] = order.number
    return Reference(suppliers, orders, codes, numbers)
