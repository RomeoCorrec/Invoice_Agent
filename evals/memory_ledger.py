"""The dataset's reference data behind the four lookups the explaining model may use.

It answers like the ledger does for a single document: suppliers and open purchase orders are
there, and no invoice has been booked. The tools themselves (their schemas and their handlers)
are the production ones; only what they read from is replaced, so an evaluation needs no
database.
"""

from evals.context import Reference
from invoice_agent.tools.errors import ToolRefusedError
from ledger.schemas import (
    BookedInvoice,
    PurchaseOrderInfo,
    PurchaseOrderLineInfo,
    SupplierInfo,
)


class MemoryLedger:
    def __init__(self, reference: Reference) -> None:
        self._reference = reference

    def get_supplier(
        self, *, siren: str | None = None, vat_number: str | None = None
    ) -> SupplierInfo | None:
        if siren is None and vat_number is None:
            raise ToolRefusedError("invalid_request", "give a siren or a vat_number")
        for supplier in self._reference.suppliers:
            if (siren is not None and supplier.siren == siren) or (
                vat_number is not None and supplier.vat_number == vat_number
            ):
                return SupplierInfo(
                    code=supplier.code,
                    name=supplier.name,
                    siren=supplier.siren,
                    siret=supplier.siret,
                    vat_number=supplier.vat_number,
                    iban_masked=supplier.iban.masked if supplier.iban else None,
                    default_expense_account=supplier.default_expense_account,
                    supplier_account=supplier.supplier_account,
                )
        return None

    def list_open_purchase_orders(self, supplier_code: str) -> list[PurchaseOrderInfo]:
        codes = self._reference.supplier_codes
        return [
            self._info(order.number)
            for order in self._reference.orders
            if codes[order.supplier_id] == supplier_code and order.status.value == "open"
        ]

    def get_purchase_order(self, number: str) -> PurchaseOrderInfo | None:
        known = {order.number for order in self._reference.orders}
        return self._info(number) if number in known else None

    def find_invoices(
        self, supplier_siren: str, invoice_number: str | None = None
    ) -> list[BookedInvoice]:
        return []  # a single-document evaluation has no history

    def _info(self, number: str) -> PurchaseOrderInfo:
        [order] = [o for o in self._reference.orders if o.number == number]
        return PurchaseOrderInfo(
            number=order.number,
            supplier_code=self._reference.supplier_codes[order.supplier_id],
            order_date=order.order_date,
            status=order.status.value,
            total_ht=order.total_ht,
            lines=tuple(
                PurchaseOrderLineInfo(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    amount_ht=line.amount_ht,
                )
                for line in order.lines
            ),
        )
