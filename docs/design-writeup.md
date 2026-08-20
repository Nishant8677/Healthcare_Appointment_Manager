# System design

The requirements that shaped this system are not features; they are failure modes. Two
patients pressing "book" in the same instant. A doctor going on leave over a day that is
already full. An email provider that is down. Each is rare enough to survive a demo and common
enough to happen in production, so the question throughout was never "how do we do this" but
"what happens when this goes wrong".

## Preventing double-booking

Two patients can request the same slot within the same millisecond. The obvious defence —
check whether it is free, then insert — is a race, and the window between those two statements
is where the second request lands.

A row lock cannot close it. `SELECT … FOR UPDATE` locks rows that exist, and the contended row
does not exist yet: both transactions are trying to create it. There is nothing to lock.

So the arbiter is a **partial unique index** on `(doctor_profile_id, starts_at)`, covering
only the statuses that occupy a slot — held, confirmed, completed. Concurrent inserts all
reach PostgreSQL, exactly one commits, and the rest raise an integrity error that becomes a
clean `409`. Cancelled appointments fall outside the index, so a cancellation frees the slot
without any cleanup step.

The availability check still runs first, but it is explicitly a courtesy that produces a
better error most of the time — not the guarantee. The guarantee lives in the database, where
no service, script or future endpoint can forget it. A test fires twenty simultaneous requests
at one slot and asserts exactly one `201`, nineteen `409`s, no `500`s, and exactly one
occupying row.

Operations on an appointment that *does* exist — confirming, cancelling, rescheduling — take a
real row lock and re-read state inside the transaction, because there the row is there to
lock. Two different races, two different mechanisms. Conflating them is the usual way this
ends up looking correct while being wrong.

## The slot hold

A patient needs time to describe their symptoms without losing the slot, and a slot cannot sit
reserved indefinitely for someone who closed the tab.

Booking is therefore two steps. `hold` creates the appointment in a `HELD` state with an
expiry a few minutes out; `confirm` submits the symptom form and promotes it. Because `HELD`
is inside the unique index, a hold blocks other patients exactly as a confirmed booking does —
the reservation is real, not advisory.

Expired holds are reclaimed **lazily**, when another patient tries to take the slot, rather
than by a sweeper job. Availability treats a lapsed hold as free, and the booking path deletes
it inside the same transaction that creates the replacement. A sweeper would be a second
moving part with its own schedule and failure modes, bought for tidiness nobody observes.

The portal shows the countdown, red under a minute, and replaces the form with an explanation
when it lapses. The API enforces the rule regardless; the countdown only makes it legible.

## Doctor leave over existing bookings

Marking a doctor away on a day that already has patients is destructive, so the API refuses by
default. It answers `409` with the number of appointments involved and a read-only endpoint
listing exactly who is affected, by name and time. Only a request carrying
`cancel_existing_appointments=true` proceeds.

That is a deliberate second step rather than friction for its own sake: cancelling several
people's medical appointments should never be a side effect of recording a date. The admin
portal makes the acknowledgement informed — choosing a date lists the affected patients and
relabels the button to "Cancel 3 appointments and record leave".

When it does proceed, everything commits together: the leave day, every cancellation, every
patient's notice, and the removal of reminders for appointments that are no longer happening.
The day is either fully handled or untouched.

## When notifications fail

Nothing is ever sent during the request that caused it. A booking writes its confirmation rows
in the *same transaction* as the appointment — a transactional outbox — so the two cannot
disagree. If the booking rolls back the messages vanish with it; if it commits, delivery is
guaranteed to be queued.

A background worker delivers them, claiming rows with `FOR UPDATE SKIP LOCKED` so a second
instance takes different work rather than sending the same email twice. Failures retry with
growing backoff. A message that exhausts its budget is parked as `failed` rather than deleted,
because one that silently vanished is indistinguishable from one never raised — and the admin
portal exists to surface those rows and requeue them once the cause is fixed.

The same shape covers the two other external dependencies. An AI summary is *requested* inside
the booking transaction and generated afterwards; a calendar entry is recorded as desired state
and reconciled afterwards. In every case the intent is durable and transactional while the
fulfilment is allowed to fail and retry — which is why, verified against a running server with
a deliberately invalid model key, the booking still returned `200`.
