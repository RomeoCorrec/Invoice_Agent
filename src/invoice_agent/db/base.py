"""Declarative base with deterministic constraint names.

Explicit names make migrations readable and let a failed constraint be recognised
by name in error messages.
"""

from enum import StrEnum

from sqlalchemy import MetaData, Numeric
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


MONEY = Numeric(12, 2)
QUANTITY = Numeric(12, 4)
RATE = Numeric(5, 2)


def in_values(column: str, enum: type[StrEnum]) -> str:
    """SQL ``column IN ('a', 'b')`` built from an enum, for CHECK constraints."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"
