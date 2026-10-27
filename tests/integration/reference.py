"""The dataset's reference data (suppliers, purchase orders) loaded into the database."""

from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from invoice_agent.db.reference_data import load_reference_data
from tests.factories import IBAN_KEY

DATASET = Path(__file__).parents[1] / "fixtures" / "invoice-test-set"


def load_reference(session: Session) -> dict[str, UUID]:
    """Insert suppliers and purchase orders; return supplier id by code."""
    return load_reference_data(session, DATASET / "reference", IBAN_KEY)
