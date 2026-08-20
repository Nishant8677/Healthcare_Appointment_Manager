# ADR 0006 — The doctor leave cascade

**Status:** Accepted · **Phase:** 5

Marking a doctor unavailable on a day they already have patients booked. This is where the
schema, the booking rules and the outbox all have to work together, and the failure mode is
not a crash — it is a patient turning up to a clinic that is closed.

---

## 1. Recording leave over existing bookings is refused by default

**Decision.** `POST /admin/doctors/{id}/leave` returns `409` when appointments exist on that
date, and changes nothing. The admin must resend with `cancel_existing_appointments: true`.

**Why.** The other option is to cancel silently and report the number afterwards, which makes
cancelling several people's medical appointments a *side effect* of recording a date. An admin
fixing a typo in a date, or resubmitting a form, should not be able to do that by accident.

The refusal is not a dead end: the error names the count and points at the preview endpoint,
so the recovery path is one request away. This is the "confirm before something irreversible"
rule applied inside the API rather than left to whatever UI happens to call it — a second
client, or a curl command, gets the same protection.

## 2. There is a preview endpoint, not just a count

**Decision.** `GET /admin/doctors/{id}/leave/impact?date=` lists the affected appointments
with patient names and times. It is read-only.

**Why.** "2 appointments will be cancelled" is not enough information to make that decision.
Whether to proceed depends on *who* — a routine follow-up and a post-operative check are very
different calls, and the admin is the only one who can weigh that. A count tells them
something is at stake; the list tells them what.

## 3. The whole cascade is one transaction

**Decision.** The leave day, every cancellation, every queued notification and every dropped
reminder commit together.

**Why.** The partial outcomes are all bad in distinct ways. Leave recorded but appointments
still live means patients arrive to a closed clinic. Appointments cancelled but no leave
recorded means the slots are immediately rebookable. Cancellations without notifications is
the worst of the three, because it looks like success. One transaction removes all of them:
either the day is fully handled or nothing happened.

This is only possible because notifications are rows rather than API calls (ADR 0005). A
cascade that sent emails inline could not be atomic at all — there is no rolling back an
email.

## 4. Held slots are released but their holders are not emailed

**Decision.** `HELD` appointments on the leave day are cancelled along with confirmed ones,
but generate no message.

**Why.** A hold is a patient part-way through choosing, not a booking. They have not been told
an appointment exists, so telling them one has been cancelled would be confusing — the
correct experience is simply finding the slot gone when they try to confirm, which the
existing `409`/`410` handling already gives them.

## 5. Only the patient is told, not the doctor

**Decision.** The `LEAVE_CONFLICT` notice goes to the affected patient. The doctor gets
nothing.

**Why.** The doctor is the reason the leave exists. A message per cancelled appointment would
be a stack of notifications about something they just arranged. The admin sees the totals in
the response, which is where that information is actually useful.

## 6. Removing a leave day does not restore the appointments

**Decision.** `DELETE .../leave/{id}` frees the slots for new bookings but leaves the
cancelled appointments cancelled.

**Why.** Those patients have already been told their appointment is off, and many will have
made other arrangements. Silently reinstating an appointment somebody believes is cancelled is
worse than making them rebook — the failure mode is a patient who does not turn up, versus a
patient who books again. Restoring them would also have to re-run availability checks, since
the slots may have been taken in the meantime.

## 7. "That day" is a local-calendar question

**Decision.** Affected appointments are found by bracketing a generous UTC window and matching
the exact local date in Python.

**Why.** Appointments are stored as UTC instants but leave is a calendar date in the clinic's
timezone, and the two do not line up. Composing the boundary in SQL is possible but harder to
read, and a midnight that does not exist on a daylight-saving day would make it subtly wrong.
Filtering the candidates in Python is exact and obvious, and the window is small enough that
the extra rows are irrelevant.
