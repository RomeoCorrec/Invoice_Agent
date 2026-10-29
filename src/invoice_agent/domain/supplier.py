from uuid import UUID

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.iban import IbanFingerprint
from invoice_agent.domain.types import AccountNumber, EmailAddress, NonEmptyStr, Siren, Siret


class Supplier(DomainModel):
    id: UUID
    code: NonEmptyStr
    name: NonEmptyStr
    siren: Siren
    siret: Siret
    vat_number: str | None = None
    iban: IbanFingerprint | None = None
    default_expense_account: AccountNumber
    """PCG account used for the debit line, e.g. ``6064``."""
    supplier_account: AccountNumber
    """Auxiliary payable account used for the credit line, e.g. ``401MERC``."""
    contact_email: EmailAddress | None = None
    """Where the supplier is written to. The only source of a recipient: never a document."""
