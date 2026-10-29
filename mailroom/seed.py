"""Load the contacts the mailroom may write to: one address per supplier of the reference data.

In production the contacts would be synchronised from the supplier referential. The agent keeps
the address in its own referential too, and the mailroom checks it again: two systems that
have to agree before a message leaves.
"""

import json
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invoice_agent.db.reference_data import demo_contact_address
from mailroom import tables

DEFAULT_REFERENCE_DIR = (
    Path(__file__).parents[1] / "tests" / "fixtures" / "invoice-test-set" / "reference"
)


def seed_contacts(engine: Engine, reference_dir: Path = DEFAULT_REFERENCE_DIR) -> int:
    """Add or update one contact per supplier. Messages already stored are never touched."""
    suppliers = json.loads((reference_dir / "suppliers.json").read_text(encoding="utf-8"))
    rows = [
        {"supplier_siren": s["siren"], "address": demo_contact_address(s["name"])}
        for s in suppliers
    ]
    with engine.begin() as connection:
        statement = pg_insert(tables.contacts)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["supplier_siren"], set_={"address": statement.excluded.address}
            ),
            rows,
        )
    return len(rows)
