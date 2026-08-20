"""Domain errors.

Services raise these; the API layer decides what HTTP status each becomes. Keeping the two
separate means the booking rules stay testable without a web request, and a single error
cannot drift into meaning two different status codes at two different call sites.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, business-rule failures."""


class EmailAlreadyRegistered(DomainError):
    """An account already exists for that address."""


class InvalidCredentials(DomainError):
    """Email not found, or password does not match.

    Deliberately one error for both cases: distinguishing them would let an attacker
    enumerate which email addresses are registered with the clinic.
    """


class InactiveAccount(DomainError):
    """The account exists but has been deactivated."""
