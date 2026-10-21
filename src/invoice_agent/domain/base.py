"""Base class shared by every domain model."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable model that rejects unknown fields.

    Immutability means a validated object stays valid: changes go through methods that
    build (and therefore re-validate) a new instance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
