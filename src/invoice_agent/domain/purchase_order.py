from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field, computed_field

from invoice_agent.domain.base import DomainModel
from invoice_agent.domain.types import Money, NonEmptyStr, PositiveMoney, Quantity


class PurchaseOrderStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PurchaseOrderLine(DomainModel):
    description: NonEmptyStr
    quantity: Quantity
    unit_price: PositiveMoney
    amount_ht: Money


class PurchaseOrder(DomainModel):
    id: UUID
    number: NonEmptyStr
    supplier_id: UUID
    order_date: date
    status: PurchaseOrderStatus = PurchaseOrderStatus.OPEN
    lines: tuple[PurchaseOrderLine, ...] = Field(min_length=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_ht(self) -> Money:
        return sum((line.amount_ht for line in self.lines), start=Decimal("0.00"))
