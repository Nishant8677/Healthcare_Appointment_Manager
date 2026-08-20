# ADR 0004 — Booking and double-booking prevention

**Status:** Accepted · **Phase:** 3

The centre of the project. This records how two patients are prevented from taking the same
slot, and why the mechanism is not the one people usually reach for.

---

## 1. Two different races need two different mechanisms

**Decision.** Creating a hold is protected by the **partial unique index**. Confirming,
cancelling and rescheduling are protected by **`SELECT ... FOR UPDATE`**.

**Why.** These are usually conflated, and that is precisely how booking code ends up looking
correct while being wrong.

When two patients try to *create* a booking for the same free slot, there is no row to lock.
`SELECT ... FOR UPDATE` locks rows that exist; it cannot reserve the absence of one. A
check-then-insert therefore always has a window: both requests read "free", both insert. The
guarantee has to come from the database refusing the second write, which is what the partial
unique index on `(doctor_profile_id, starts_at)` does. Concurrent inserts all reach Postgres,
exactly one commits, the rest raise `IntegrityError`, and that becomes a 409.

The availability check that runs beforehand is deliberately *not* the guarantee. It exists to
produce a friendlier error in the common case; the correctness argument does not depend on it.
That distinction is written into the module docstring so a later reader does not "optimise"
the index away.

When *confirming*, the row does exist, so the row lock is the right tool. Without it two
simultaneous confirms could both read `held` before either wrote, attaching two symptom forms
to one appointment.

**Proven, not asserted.** `tests/test_booking_concurrency.py` fires twenty concurrent hold
requests from twenty distinct patients at one slot, each through its own client, and asserts
exactly one `201`, nineteen `409`, no `500`, and exactly one occupying row in the database.
The result is deterministic despite arbitrary timing: the index permits at most one row and
at least one insert must succeed. A parallel test does the same for concurrent confirms.

## 2. Slots are computed, never stored

**Decision.** Availability is derived on request from working hours minus leave minus
bookings. There is no `slots` table.

**Why.** Materialising a year of empty slots per doctor would need a job to extend the
horizon, a backfill whenever hours change, and a large table that is overwhelmingly "nothing
booked here". Deriving them means a schedule change is correct immediately and retroactively,
with no regeneration step and no possibility of stale rows disagreeing with the doctor's
actual hours.

## 3. A requested time must match a generated slot

**Decision.** `POST /appointments/hold` regenerates the day's slots and requires the requested
`starts_at` to be one of them.

**Why.** Without it, a patient could book 03:17 by posting a handcrafted timestamp. The
endpoint accepts a time, so it must verify that time is one the clinic actually offers rather
than trusting the client to have picked from the list it was shown.

## 4. Hold, then confirm

**Decision.** Booking is two steps: a short reservation, then confirmation with the symptom
form. The hold lives for `SLOT_HOLD_MINUTES` (default 5).

**Why.** The assignment requires symptoms *before* confirmation. Without a reservation the
slot could be taken while the patient is typing — the worst possible moment to lose it, and a
race the confirm step could only ever lose. The hold converts that into a bounded reservation
whose cost, if abandoned, is a few minutes of one slot.

## 5. An expired hold is reclaimed at booking time, not by a sweeper

**Decision.** No background job expires holds. The availability query ignores holds whose
`hold_expires_at` has passed, and the booking path deletes a lapsed hold on that exact slot
inside the same transaction as the insert.

**Why.** Two mechanisms are needed because they see the row differently. The availability
query can simply filter on the expiry — an abandoned hold stops blocking the slot the instant
it lapses. The unique index *cannot*: its predicate matches on status alone, so a stale `held`
row keeps occupying the index and would block the slot forever. Deleting it immediately
before the insert closes that gap without a scheduled job.

It is deleted rather than kept as a tombstone because a hold that was never confirmed is a
transient reservation, not clinical history — it carries no symptom form and no clinical
meaning.

## 6. One live hold per patient

**Decision.** A patient with an unexpired hold cannot take another.

**Why.** Otherwise one patient could reserve a doctor's entire day and release none of it —
denial of service through the booking form. The error names the existing appointment so the
client can recover rather than dead-end.

## 7. Someone else's appointment is "not found", not "forbidden"

**Decision.** Acting on an appointment belonging to another patient returns `404`, identical
to an id that does not exist.

**Why.** A distinct `403` would confirm that the id is real and belongs to somebody — turning
the endpoint into an oracle for enumerating appointments. The ids are UUIDs specifically to
resist enumeration; leaking existence through the status code would undo that.

## 8. An expired hold returns `410`, not `409`

**Decision.** Confirming a lapsed hold is `410 Gone`. Losing a race is `409 Conflict`.

**Why.** They demand different client behaviour. A `409` invites a retry — the world moved,
try again. A `410` means this specific thing is finished and the correct move is to start over
from slot selection. Collapsing both into `409` would push that judgement onto every client.

## 9. Rescheduling is one transaction, not two requests

**Decision.** `POST /appointments/{id}/reschedule` cancels the original and creates the
replacement in a single transaction, carrying the symptom form across, and returns the new
appointment.

**Why.** Done client-side as cancel-then-book, a failure between the two calls leaves the
patient with no appointment at all and their old slot gone — the worst outcome available. In
one transaction the patient always has exactly one appointment. If the new slot is taken in
the meantime the whole thing rolls back and the original stands.

The symptom form is copied because the complaint has not changed just because the time did;
making the patient retype it would be a needless invitation to lose information.

## 10. Wall-clock hours, UTC instants

**Decision.** Working hours are stored as bare times and interpreted in `CLINIC_TIMEZONE`.
Appointments are stored as `timestamptz` — real instants. Conversion happens once, at slot
generation.

**Why.** "The clinic opens at nine" is a wall-clock fact that stays true across daylight-saving
changes; an appointment is a specific moment that must not drift. Storing each as what it
actually is avoids the classic bug where a schedule silently shifts by an hour twice a year.

`combine_in_zone` returns `None` for a local time that does not exist — the hour skipped by a
spring-forward — and those slots are dropped rather than silently shifted into a neighbouring
hour. Offering an appointment at a moment that never happens would produce a booking nobody
could attend.

## 11. A booking horizon

**Decision.** `BOOKING_HORIZON_DAYS` (default 60) bounds how far ahead a patient can book.

**Why.** Slot generation is unbounded otherwise, and a booking made two years out would rest
on working hours nobody has decided yet. It also bounds the cost of a request that asks for
availability far in the future.
