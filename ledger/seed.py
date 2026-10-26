"""Load the reference data (suppliers, purchase orders, chart of accounts) into the ledger."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, delete, insert

from invoice_agent.domain.iban import mask_iban
from ledger import tables

DEFAULT_REFERENCE_DIR = (
    Path(__file__).parents[1] / "tests" / "fixtures" / "invoice-test-set" / "reference"
)


def seed_reference(engine: Engine, reference_dir: Path = DEFAULT_REFERENCE_DIR) -> None:
    """Replace the reference tables' content. Posted entries are never touched."""
    chart = json.loads((reference_dir / "chart_of_accounts.json").read_text(encoding="utf-8"))
    suppliers = json.loads((reference_dir / "suppliers.json").read_text(encoding="utf-8"))
    orders = json.loads((reference_dir / "purchase_orders.json").read_text(encoding="utf-8"))
    with engine.begin() as connection:
        for table in (
            tables.purchase_order_lines,
            tables.purchase_orders,
            tables.suppliers,
            tables.accounts,
        ):
            connection.execute(delete(table))
        connection.execute(
            insert(tables.accounts),
            [{"number": number, "label": label} for number, label in chart.items()],
        )
        connection.execute(
            insert(tables.suppliers),
            [
                {
                    "code": s["code"],
                    "name": s["name"],
                    "siren": s["siren"],
                    "siret": s["siret"],
                    "vat_number": s.get("vat_number"),
                    "iban_masked": mask_iban(s["iban"]) if s.get("iban") else None,
                    "default_expense_account": s["default_expense_account"],
                    "supplier_account": s["supplier_account"],
                }
                for s in suppliers
            ],
        )
        connection.execute(
            insert(tables.purchase_orders),
            [
                {
                    "number": o["number"],
                    "supplier_code": o["supplier_code"],
                    "order_date": date.fromisoformat(o["order_date"]),
                    "status": o["status"],
                    "total_ht": o["total_ht"],
                }
                for o in orders
            ],
        )
        connection.execute(
            insert(tables.purchase_order_lines),
            [
                {
                    "po_number": o["number"],
                    "position": position,
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                    "amount_ht": line["amount_ht"],
                }
                for o in orders
                for position, line in enumerate(o["lines"], start=1)
            ],
        )
