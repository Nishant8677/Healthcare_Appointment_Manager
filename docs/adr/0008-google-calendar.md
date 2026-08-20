# ADR 0008 — Google Calendar sync

**Status:** Accepted · **Phase:** 7

Appointments have to appear on the patient's and the doctor's real calendars, move when a
booking is rescheduled, and disappear when it is cancelled. The interesting decisions are not
about calling an API — that part is four HTTP requests — but about what the queue between the
booking and Google is allowed to represent.

---

## 1. The queue holds desired state, not commands

**Decision.** There is at most one `calendar_sync_jobs` row per `(appointment_id, user_id)`,
enforced by a unique constraint. Enqueueing a change **overwrites** it. The `action` column
says what the calendar should end up showing — `sync` or `delete` — not what operation to run.

**Why.** This is the decision the rest of the phase hangs from.

A command queue is the obvious design and it has a real defect. Book an appointment, then
cancel it a moment later, and the queue holds a *create* followed by a *delete*. Two workers
polling with `SKIP LOCKED` can take one each, and nothing guarantees the create finishes
first. If the delete runs first it deletes nothing (the event does not exist yet), the create
then succeeds, and the outcome is a live calendar entry for an appointment that was cancelled
— on a real person's phone, for a consultation nobody will attend. It is a rare interleaving,
which is exactly what makes it dangerous: it survives testing and appears in production.

Making the row a *goal* removes the failure rather than defending against it. "This
appointment should not be on your calendar" written over "this appointment should be on your
calendar" cannot be applied in the wrong order, because there is only ever one statement. The
worker's job stops being "replay these operations" and becomes "make Google agree with this
row" — which is also a much easier thing to reason about when it fails halfway.

The cost is that intermediate states are not preserved: an appointment moved three times before
the worker runs is written to Google once, at its final time. That is a feature.

## 2. Event ids are derived, not assigned by Google

**Decision.** The Google event id is `base32hex(sha256(appointment_id:user_id))` — computed
before the first request, identical on every retry.

**Why.** Letting Google assign an id and storing it has a window that cannot be closed: between
Google committing the event and the id reaching our database, a crash or a lost response
orphans an event that nothing can now find or delete. The usual mitigation is to hunt for
duplicates afterwards, which needs the read scope this app deliberately does not request.

Choosing the id makes creation **idempotent**. A create that timed out after Google committed
it can be retried safely: the retry addresses the same event. Google answers `409` for a
duplicate id and `404` for an update to a missing one, so the gateway self-heals in both
directions — insert-then-update on a 409, update-then-insert on a 404. Neither path can
produce two events for one appointment.

The id is a hash rather than the raw ids because it is visible in a user's calendar export.
An appointment id in a calendar entry is an identifier for this API; a hash is not.

## 3. Separate events, not one event with attendees

**Decision.** Each participant gets their own event on their own calendar. Nobody is listed as
an attendee.

**Why.** The attendee approach looks tidier and behaves worse. Listing the doctor as an
attendee on the patient's event makes Google *invite* them, which puts a second entry on the
doctor's calendar next to the one this app wrote directly, and sends a Google invitation email
alongside the clinic's own confirmation. Two direct writes are quieter and give each side a
description written for them — the patient sees the doctor's name and specialisation, the
doctor sees the patient's name and contact.

It also means the two connections fail independently. A doctor whose grant has lapsed does not
stop the patient's calendar entry from being written.

## 4. The calendar event never carries the symptom report

**Decision.** Events carry the time, the other party's name and the specialisation. The
patient's description of their symptoms is not included.

**Why.** A calendar entry syncs to phones, watches, and whatever else the account is signed
into, and is routinely visible to anyone glancing at a screen. It is the wrong surface for a
description of somebody's medical complaint, however convenient it would be for the doctor —
who has the full symptom report and the pre-visit brief in the app, where access is
controlled.

## 5. Refresh tokens are encrypted at rest, and that is worth a dependency

**Decision.** Refresh tokens are Fernet-encrypted with a key from `CALENDAR_TOKEN_KEY` before
they reach a column. This is the only new dependency in the phase (`cryptography`).

**Why.** A refresh token is not analogous to a password hash. A leaked hash is a puzzle an
attacker still has to solve; a leaked refresh token is *working access to somebody's calendar*,
redeemable immediately and valid until they notice and revoke it. Keeping the key in the
environment rather than the database means a stolen dump is useless on its own.

The submission guidance asks for minimal dependencies, and this phase respects that overall:
the Google client libraries would have added roughly ten transitive packages to wrap four HTTP
requests, and the REST API is used directly instead. But "minimal dependencies" means not
pulling in a framework where the standard library suffices — Python has no cipher in its
standard library, and the alternative to one vetted crypto package is hand-rolled cipher code.
That is the one case where writing it yourself is strictly the less safe option.

Access tokens, which live an hour, are cached **in process memory** and never written to disk —
keyed by a hash of the refresh token rather than the token itself, so the raw secret is not a
dictionary key that could surface in a heap dump.

## 6. The OAuth `state` is a signed token, not a database row

**Decision.** `state` is a short-lived JWT carrying the user id, with `type:
"calendar_oauth_state"`, signed with the application's existing key.

**Why.** Google redirects the *browser* back to the callback, so the request arrives with no
`Authorization` header and the endpoint has no idea who it is for. The conventional answer is a
table of pending state values with a cleanup job. A signed token carries the identity in a form
that cannot be forged, expires on its own, and needs no storage.

The signature is doing real security work, not ceremony. Without it, anyone could call the
callback with their own Google authorisation code and a chosen state, and attach a calendar
they control to another person's clinic account — including a doctor's, thereby receiving a
copy of that doctor's entire schedule. The explicit `type` claim matters for the same reason:
both tokens are signed with the same key, so without it a stolen access token could be pasted
into a callback URL. There is a test for each of those attacks.

## 7. One job per transaction, unlike the notification worker

**Decision.** The calendar worker claims, processes and commits **one** row at a time. The
notification worker processes a whole batch under a single commit.

**Why.** Not a style inconsistency — the tables are written differently. Notification rows are
only ever *inserted* from a request path, so a batch-long row lock inconveniences nobody.
Calendar rows are *updated* from a request path: cancelling an appointment rewrites its sync
row. Holding twenty rows locked across twenty round trips to Google would make a patient's
cancellation block on Google's latency. Locking one row for one HTTP call bounds that wait to a
single request.

`SKIP LOCKED` still does the multi-instance work in both.

## 8. "No calendar connected" is skipped, not failed

**Decision.** Four states: `pending`, `synced`, `skipped`, `failed`. A user with no connection
— or one they revoked — produces `skipped` with a reason.

**Why.** The overwhelming majority of a clinic's patients will never connect a calendar, and a
user who disconnects is exercising a supported choice. Recording either as a failure would bury
the handful of rows that represent a genuine problem under thousands that do not, and make the
admin dashboard's only actionable number useless. `failed` is the number worth watching, and it
is small by construction.

Nothing is enqueued at all for a participant without a connection, so a clinic that never
configures Google accumulates an empty table rather than a permanent trail of skipped rows.

## 9. Connecting backfills existing appointments

**Decision.** Connecting a calendar queues that user's upcoming appointments, bounded by
`CALENDAR_BACKFILL_LIMIT`.

**Why.** Without it, connecting affects only appointments booked from that moment on. The first
person to try it connects, sees nothing appear, and concludes the feature does not work. The
bound is there because "queue everything" for a doctor with a full year of bookings is an
unbounded amount of work triggered by one click.

## 10. Rescheduling moves the event rather than replacing it

**Decision.** A reschedule re-points the existing sync row — event id and synced state included
— at the replacement appointment.

**Why.** A reschedule creates a new appointment row and cancels the old one, so the natural
implementation deletes one event and creates another. On a phone that reads as a cancellation
followed by a surprise booking, with two notifications, and it briefly leaves the patient with
no appointment on their calendar at all. Carrying the event id across makes the entry *move*,
the way it would if the user had dragged it — one update, one notification, no gap.

## 11. Google being unconfigured is a supported state

**Decision.** With `GOOGLE_CLIENT_ID` unset, `build_oauth_client` returns `None`, the connect
endpoints answer `503` with an explanation, and no sync row is ever written. A half-configured
integration — a client id with no secret or no encryption key — fails at **startup**.

**Why.** These are opposite kinds of missing. Nothing configured is a deliberate deployment
choice, and the rest of the system is unaffected by it. A client id with no encryption key is a
mistake that would otherwise surface at the callback — *after* the user has already granted
consent, which is the worst possible moment to discover a missing environment variable.

This mirrors the LLM boundary's rule (ADR 0007 §7), with one deliberate difference: the LLM
falls back loudly because a stub clinical summary reaching a screen would be dangerous. A
missing calendar entry is not, so the calendar gateway falls back to an in-memory
implementation quietly, and the sync row records exactly what happened.
