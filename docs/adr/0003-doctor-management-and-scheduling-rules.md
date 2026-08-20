# ADR 0003 — Doctor management and scheduling rules

**Status:** Accepted · **Phase:** 2

---

## 1. Scheduling rules are pure functions, separate from the service

**Decision.** `app/services/scheduling.py` contains only functions over plain values —
`WorkingWindow`, `time`, integers. No session, no ORM, no HTTP. The doctor service is the
imperative shell that loads rows, applies those rules and writes the result.

**Why.** Slot arithmetic is the part of this system most likely to be subtly wrong, and it is
about to be reused by Phase 3's slot generation. Keeping it pure means it can be tested
exhaustively — every boundary, every awkward duration — in milliseconds and without a
database. It also means Phase 3 can generate slots without duplicating the definition of a
valid working window.

## 2. A working window must divide exactly into whole appointments

**Decision.** 09:00–17:00 with 45-minute slots is rejected, not silently truncated to ten
appointments with 30 unusable minutes at the end.

**Why.** The alternative is a remainder that exists in the data but can never be booked —
invisible in the admin screen, and a source of "why can't I book 16:30?" questions later.
Rejecting it forces the ambiguity to be resolved once, by the person who knows the clinic's
intent, instead of being resolved silently and wrongly by the slot generator.

The cost is a stricter admin screen, which is why the error does the arithmetic for them:

> Monday 09:00-17:00 is 480 minutes, which is not a whole number of 45-minute appointments.
> Try ending at 16:30 or 17:15.

An error that only said "does not divide evenly" would technically be correct and practically
useless.

## 3. Touching windows are allowed; overlapping ones are not

**Decision.** 09:00–12:00 followed by 12:00–17:00 is valid. 09:00–13:00 with 12:00–17:00 is
rejected.

**Why.** Two windows meeting at a boundary is how a continuous day gets expressed when the
clinic wants it recorded as two blocks. Sharing an actual minute is different: it would
generate the same slot twice, which the database's unique index would then reject at booking
time — a confusing failure surfacing hours later, in someone else's request.

## 4. Working hours are replaced wholesale, not edited row by row

**Decision.** `PUT /admin/doctors/{id}/working-hours` takes the complete weekly schedule.
There is no endpoint to add or delete a single window.

**Why.** Overlap is a property of the whole set — no single window can be validated in
isolation. With incremental edits, every intermediate state has to be legal, which either
forces admins into a specific edit order or requires temporarily invalid states. A full
replacement is validated once against the complete picture, is idempotent, and is atomic:
a rejected replacement leaves the previous schedule untouched, which has its own test.

## 5. Changing the slot duration re-validates the existing schedule

**Decision.** `PATCH` with a new `slot_duration_minutes` re-runs validation over the doctor's
current working hours and refuses if they no longer divide evenly.

**Why.** This is the cross-cutting rule that is easy to miss. Each field is individually
valid — the schedule was fine, the new duration is within bounds — but the *combination* is
not. Without this check, switching a doctor from 30- to 45-minute appointments would quietly
strand a fragment of every working day.

## 6. Leave is recorded without a conflict cascade, for now

**Decision.** Phase 2 records leave days. Cancelling affected appointments and notifying
those patients is Phase 5.

**Why.** Safe by construction at this point: appointments cannot be created until Phase 3, so
there is nothing to conflict with. Sequencing it this way keeps the notification cascade in
the same phase as the outbox that makes it reliable, rather than writing a fire-and-forget
version now and rewriting it later.

## 7. A leave day is scoped to its doctor in the query, not just the URL

**Decision.** `DELETE /admin/doctors/{doctor_id}/leave/{leave_id}` filters on *both* ids.

**Why.** Filtering only by `leave_id` would let a correctly-formed request delete another
doctor's leave by pairing a valid leave id with the wrong doctor id — the classic insecure
direct object reference. The URL expresses ownership, so the query must enforce it. Tested
explicitly.

## 8. Doctors are deactivated, never deleted

**Decision.** `PATCH` sets `is_active`. Listings hide inactive doctors unless
`include_inactive=true`. There is no delete endpoint.

**Why.** Appointments and prescriptions must stay attributable after a doctor leaves the
clinic — the foreign keys use `ON DELETE RESTRICT` precisely so that erasing clinical history
fails loudly rather than cascading.

## 9. The first admin comes from a CLI, and its password comes from the environment

**Decision.** `python -m app.cli create-admin --email … --name …`, reading the password from
`ADMIN_PASSWORD`.

**Why.** A genuine bootstrapping gap: admins deliberately cannot self-register, and only an
admin can create accounts — so without this the entire admin API is unreachable on a fresh
deployment. The password is read from the environment rather than a `--password` flag because
a flag is visible in shell history and to anyone who can list processes on the machine.

## 10. The router carries the role guard, not each route

**Decision.** `APIRouter(..., dependencies=[Depends(require_roles(UserRole.ADMIN))])`.

**Why.** A guard declared per route is a guard that can be forgotten on the next route added,
and its absence looks exactly like ordinary code. Declaring it once on the router makes
protection the default for everything mounted there.
