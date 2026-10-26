"""The mock accounting system's storage and rules.

Reads return what the ledger knows; the only write, ``post_entry``, is idempotent: the same
key with the same request returns the entry already written, the same key with another
request is refused, and a supplier invoice can be booked once whatever the key.
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ledger import tables
from ledger.schemas import (
    AccountInfo,
    BookedInvoice,
    EntryRequest,
    ErrorCode,
    LedgerRefusal,
    PostedEntry,
    PurchaseOrderInfo,
    PurchaseOrderLineInfo,
    SupplierInfo,
)


def request_fingerprint(request: EntryRequest) -> str:
    """Hash of what the request says, without the key, to tell a replay from a different request."""
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class LedgerStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads ---------------------------------------------------------------------------

    def get_supplier(
        self, *, siren: str | None = None, vat_number: str | None = None
    ) -> SupplierInfo | None:
        if siren is None and vat_number is None:
            raise LedgerRefusal(ErrorCode.INVALID_REQUEST, "give a siren or a vat_number")
        query = select(tables.suppliers)
        query = query.where(
            tables.suppliers.c.siren == siren
            if siren is not None
            else tables.suppliers.c.vat_number == vat_number
        )
        with self._engine.connect() as connection:
            row = connection.execute(query).mappings().first()
        return SupplierInfo(**row) if row else None

    def list_open_purchase_orders(self, supplier_code: str) -> list[PurchaseOrderInfo]:
        query = (
            select(tables.purchase_orders.c.number)
            .where(
                tables.purchase_orders.c.supplier_code == supplier_code,
                tables.purchase_orders.c.status == "open",
            )
            .order_by(tables.purchase_orders.c.order_date, tables.purchase_orders.c.number)
        )
        with self._engine.connect() as connection:
            numbers = connection.execute(query).scalars().all()
            orders = [self._purchase_order(connection, number) for number in numbers]
        return [order for order in orders if order is not None]

    def get_purchase_order(self, number: str) -> PurchaseOrderInfo | None:
        with self._engine.connect() as connection:
            return self._purchase_order(connection, number)

    def find_invoices(
        self, supplier_siren: str, invoice_number: str | None = None
    ) -> list[BookedInvoice]:
        entry, line = tables.entries, tables.entry_lines
        total = func.coalesce(func.sum(line.c.amount).filter(line.c.side == "debit"), 0)
        query = (
            select(
                entry.c.id.label("entry_id"),
                entry.c.supplier_siren,
                entry.c.invoice_number,
                entry.c.entry_date,
                total.label("total"),
            )
            .join(line, line.c.entry_id == entry.c.id)
            .where(entry.c.supplier_siren == supplier_siren)
            .group_by(entry.c.id)
            .order_by(entry.c.entry_date, entry.c.invoice_number)
        )
        if invoice_number is not None:
            query = query.where(entry.c.invoice_number == invoice_number)
        with self._engine.connect() as connection:
            return [BookedInvoice(**row) for row in connection.execute(query).mappings()]

    def get_chart_of_accounts(self, prefix: str = "") -> list[AccountInfo]:
        query = select(tables.accounts).order_by(tables.accounts.c.number)
        if prefix:
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(tables.accounts.c.number.like(f"{escaped}%", escape="\\"))
        with self._engine.connect() as connection:
            return [AccountInfo(**row) for row in connection.execute(query).mappings()]

    # --- the only write ------------------------------------------------------------------

    def post_entry(self, request: EntryRequest) -> PostedEntry:
        fingerprint = request_fingerprint(request)
        try:
            with self._engine.begin() as connection:
                self._check_supplier_and_accounts(connection, request)
                existing = self._find_by_key(connection, request.idempotency_key)
                if existing is not None:
                    return self._replay(request, existing, fingerprint)
                entry_id = uuid4()
                inserted = connection.execute(
                    pg_insert(tables.entries)
                    .values(
                        id=entry_id,
                        idempotency_key=request.idempotency_key,
                        request_hash=fingerprint,
                        supplier_siren=request.supplier_siren,
                        invoice_number=request.invoice_number,
                        journal=request.journal,
                        entry_date=request.entry_date,
                        created_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(tables.entries.c.id)
                ).scalar_one_or_none()
                if inserted is None:  # another request used the key between our read and write
                    raced = self._find_by_key(connection, request.idempotency_key)
                    assert raced is not None
                    return self._replay(request, raced, fingerprint)
                connection.execute(
                    insert(tables.entry_lines),
                    [
                        {
                            "entry_id": entry_id,
                            "position": position,
                            "account": line.account,
                            "label": line.label,
                            "side": line.side.value,
                            "amount": line.amount,
                        }
                        for position, line in enumerate(request.lines, start=1)
                    ],
                )
        except IntegrityError as exc:
            if "uq_entries_invoice" in str(exc.orig):
                raise LedgerRefusal(
                    ErrorCode.INVOICE_ALREADY_BOOKED,
                    f"invoice {request.invoice_number!r} of this supplier already has an entry",
                ) from None
            raise
        return PostedEntry(
            entry_id=entry_id,
            idempotency_key=request.idempotency_key,
            replayed=False,
            total=request.total_debit,
        )

    # --- helpers -------------------------------------------------------------------------

    @staticmethod
    def _purchase_order(connection: Connection, number: str) -> PurchaseOrderInfo | None:
        order = (
            connection.execute(
                select(tables.purchase_orders).where(tables.purchase_orders.c.number == number)
            )
            .mappings()
            .first()
        )
        if order is None:
            return None
        lines = (
            connection.execute(
                select(tables.purchase_order_lines)
                .where(tables.purchase_order_lines.c.po_number == number)
                .order_by(tables.purchase_order_lines.c.position)
            )
            .mappings()
            .all()
        )
        return PurchaseOrderInfo(
            number=order["number"],
            supplier_code=order["supplier_code"],
            order_date=order["order_date"],
            status=order["status"],
            total_ht=order["total_ht"],
            lines=tuple(
                PurchaseOrderLineInfo(
                    description=line["description"],
                    quantity=line["quantity"],
                    unit_price=line["unit_price"],
                    amount_ht=line["amount_ht"],
                )
                for line in lines
            ),
        )

    @staticmethod
    def _find_by_key(connection: Connection, key: str) -> tuple[UUID, str] | None:
        row = connection.execute(
            select(tables.entries.c.id, tables.entries.c.request_hash).where(
                tables.entries.c.idempotency_key == key
            )
        ).first()
        return (row.id, row.request_hash) if row else None

    @staticmethod
    def _replay(request: EntryRequest, existing: tuple[UUID, str], fingerprint: str) -> PostedEntry:
        entry_id, stored_hash = existing
        if stored_hash != fingerprint:
            raise LedgerRefusal(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "this idempotency key was already used for a different request",
            )
        return PostedEntry(
            entry_id=entry_id,
            idempotency_key=request.idempotency_key,
            replayed=True,
            total=request.total_debit,
        )

    @staticmethod
    def _check_supplier_and_accounts(connection: Connection, request: EntryRequest) -> None:
        supplier = connection.execute(
            select(tables.suppliers.c.supplier_account).where(
                tables.suppliers.c.siren == request.supplier_siren
            )
        ).first()
        if supplier is None:
            raise LedgerRefusal(ErrorCode.UNKNOWN_SUPPLIER, "no supplier with this SIREN")
        known = set(connection.execute(select(tables.accounts.c.number)).scalars())
        known.add(supplier.supplier_account)
        unknown = sorted({line.account for line in request.lines} - known)
        if unknown:
            raise LedgerRefusal(
                ErrorCode.UNKNOWN_ACCOUNT, f"accounts not in the chart: {', '.join(unknown)}"
            )
