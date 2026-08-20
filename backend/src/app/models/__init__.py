"""SQLAlchemy models.

Every model module must be imported here: Alembic autogenerate only sees tables that have
been registered on `Base.metadata` by import time.
"""

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["Base", "TimestampMixin", "UUIDPrimaryKeyMixin"]
