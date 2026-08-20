"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EnumT = TypeVar("EnumT", bound=StrEnum)


def pg_enum(enum_cls: type[EnumT], name: str) -> SAEnum:
    """A native PostgreSQL enum column type storing the member *values*.

    SQLAlchemy stores member names by default, which would put `CANCELLED_BY_CLINIC` in the
    database while the application reads `cancelled_by_clinic`. `values_callable` keeps the
    stored representation identical to what the API emits.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


# Explicit naming so Alembic autogenerate produces stable, human-readable constraint names.
# Without this, dropping a unique index in a later migration means guessing its generated name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUID primary key, generated database-side.

    UUIDs over serial ints because appointment and prescription ids appear in URLs and
    calendar payloads; sequential ids would let one patient enumerate another's records.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Creation and update timestamps, both maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
