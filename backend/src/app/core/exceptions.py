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


class DoctorNotFound(DomainError):
    """No doctor profile with that id."""


class InvalidSchedule(DomainError):
    """A working-hours or leave configuration that cannot be honoured.

    Carries a message written for the admin who has to fix it — which window clashes, or
    what end time would divide evenly — rather than a bare rule name.
    """


class DuplicateLeaveDay(DomainError):
    """That date is already recorded as leave for this doctor."""


class LeaveDayNotFound(DomainError):
    """No leave day with that id for this doctor."""


class AppointmentNotFound(DomainError):
    """No appointment with that id that this user is allowed to see.

    Deliberately also raised when the appointment exists but belongs to somebody else:
    a distinct "forbidden" would confirm that another patient holds that appointment id.
    """


class SlotUnavailable(DomainError):
    """The requested time is not a bookable slot.

    Outside the doctor's working hours, on a leave day, in the past, beyond the booking
    horizon, or already occupied when the request was checked.
    """


class SlotTaken(DomainError):
    """Another patient took the slot between the availability check and the write.

    Distinct from `SlotUnavailable` because it is a lost race rather than a bad request —
    the slot genuinely was free moments earlier.
    """


class HoldExpired(DomainError):
    """The reservation lapsed before the patient confirmed it."""


class ActiveHoldExists(DomainError):
    """This patient already holds a slot that they have not confirmed or released."""


class AppointmentNotCancellable(DomainError):
    """The appointment is already finished or cancelled."""


class AppointmentNotConfirmable(DomainError):
    """The appointment is not in a state that can be confirmed."""


class LeaveConflictsExist(DomainError):
    """Recording this leave would cancel appointments that are already booked.

    Raised so the caller has to acknowledge the number of patients affected. Cancelling other
    people's medical appointments should never be a side effect of an unrelated request.
    """

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"{count} appointment(s) already booked on that date")
