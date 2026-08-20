"""Registration and credential verification.

Plain async functions taking a session, so they can be tested directly without HTTP.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EmailAlreadyRegistered, InactiveAccount, InvalidCredentials
from app.core.security import hash_password, needs_rehash, verify_password
from app.models.enums import UserRole
from app.models.user import User

logger = logging.getLogger(__name__)


def normalise_email(email: str) -> str:
    """Addresses are compared case-insensitively; storing them lower-cased makes the unique
    constraint enforce that without a functional index."""
    return email.strip().lower()


async def register_patient(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
) -> User:
    """Create a patient account.

    Raises:
        EmailAlreadyRegistered: if the address is taken.
    """
    user = User(
        email=normalise_email(email),
        password_hash=hash_password(password),
        full_name=full_name,
        role=UserRole.PATIENT,
    )
    session.add(user)

    try:
        await session.commit()
    except IntegrityError as exc:
        # Relying on the unique constraint rather than a prior SELECT: a check-then-insert
        # would still admit a duplicate when two registrations race.
        await session.rollback()
        raise EmailAlreadyRegistered(email) from exc

    await session.refresh(user)
    logger.info("patient registered", extra={"user_id": str(user.id)})
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    """Verify credentials and return the user.

    Raises:
        InvalidCredentials: unknown address or wrong password.
        InactiveAccount: correct credentials for a deactivated account.
    """
    result = await session.execute(select(User).where(User.email == normalise_email(email)))
    user = result.scalar_one_or_none()

    if user is None:
        # Hash anyway so a missing account and a wrong password take comparable time,
        # closing a timing side channel that would reveal which addresses are registered.
        hash_password(password)
        raise InvalidCredentials(email)

    if not verify_password(password, user.password_hash):
        raise InvalidCredentials(email)

    if not user.is_active:
        raise InactiveAccount(email)

    # Transparently upgrade a hash made with older parameters, now that the plaintext is
    # available and already verified.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await session.commit()
        logger.info("password hash upgraded", extra={"user_id": str(user.id)})

    return user
