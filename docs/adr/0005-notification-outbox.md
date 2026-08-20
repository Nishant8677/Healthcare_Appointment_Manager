# ADR 0005 — The notification outbox

**Status:** Accepted · **Phase:** 4

The requirement is that a confirmation is never lost because an outside service had a bad
minute. This records how that is achieved without a message broker.

---

## 1. Notifications are rows, committed with the thing that caused them

**Decision.** Booking does not send email. It writes `notification_jobs` rows in the **same
transaction** as the appointment. A separate worker delivers them later.

**Why.** This is the whole point, and it is worth being precise about what it buys.

Sending inline has two failure modes, and they cannot both be avoided. Send *before* commit
and a later rollback means a patient was told about an appointment that does not exist. Send
*after* commit and a crash in between means an appointment exists that nobody was told about.
There is no ordering of "write row" and "call provider" that is safe, because they are two
systems with no shared transaction.

Writing the message as a row removes the second system from the critical path entirely. The
appointment and the intent to notify are one atomic write: either both happened or neither
did. Delivery becomes a separate concern that is allowed to fail and be retried, because the
record of *what to send* is already durable.

It also keeps the provider's latency out of the request. A slow provider makes the patient
wait for nothing.

## 2. A polled table, not a message broker

**Decision.** No Celery, no Redis, no RabbitMQ. A background task polls the jobs table.

**Why.** A broker would *break* the guarantee above rather than provide it: enqueueing to a
broker is a second system again, so the enqueue can succeed while the transaction rolls back,
or vice versa. The outbox pattern exists precisely because brokers cannot participate in the
database transaction.

Beyond correctness, it keeps the free-tier deployment to one service and one database, and
the dependency list within the submission guidelines. The original plan named APScheduler;
that was dropped too, because every job already carries `scheduled_for`, which makes "run
what is due" a single query and a scheduler library redundant.

## 3. Concurrency safety comes from `FOR UPDATE SKIP LOCKED`

**Decision.** The worker claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, and holds the
locks until the batch commits.

**Why.** Two API instances polling the same table would otherwise both claim the same rows and
send every message twice. `SKIP LOCKED` makes them hand each other different work instead of
blocking, so the service scales horizontally with no leader election, no distributed lock and
no configuration. Tested with two workers racing over ten jobs, asserting no address is
delivered to twice.

## 4. Retries back off, then the message is parked — never dropped

**Decision.** Failures retry after 1, 5 and 30 minutes, then hold at 30. After
`NOTIFICATION_MAX_ATTEMPTS` the job becomes `failed` and stays in the table.

**Why.** Growing gaps because a provider having a bad second deserves a quick retry and one
having a bad hour does not deserve to be hammered.

Parked rather than deleted because a vanished message is indistinguishable from one that was
never raised — "the patient says they got no email" becomes unanswerable. A `failed` row with
its `last_error` turns that into a five-second lookup, which is why
`GET /admin/notifications` and the retry endpoint exist.

## 5. One bad message must not block the queue

**Decision.** Each job is tried inside its own `try`, including a catch-all for unexpected
errors, and the batch continues.

**Why.** Without it, a single malformed payload or template bug stops delivery for everyone
behind it in the queue — a small defect becoming a total outage. An unexpected error is
treated as a delivery failure so the row is retried rather than lost.

## 6. Payloads are frozen; whether to send is not

**Decision.** Each job carries a denormalised payload and renders only from it. But when an
appointment is cancelled, its **pending reminders are deleted**.

**Why.** These look contradictory and are not. *Rendering* must be frozen: a message retried
hours later should say what it said when it was raised, not reflect rows that have since
changed. *Whether to send at all* is a question about current state, which a frozen payload
cannot answer — and a reminder for an appointment that is not happening is worse than no
reminder. Deleting the undelivered rows is more obvious than teaching the worker to re-check
every appointment before every send.

## 7. The console sender is the default everywhere but production

**Decision.** `EMAIL_PROVIDER=console` logs the message instead of sending. SendGrid is opt-in
and fails loudly at startup if selected without an API key.

**Why.** A misconfigured development environment should print emails, not mail real people.
Failing loudly on a missing key matters just as much in the other direction: silently falling
back to the console in production would look like success while no patient ever received
anything.

Two bugs found by actually reading the output rather than trusting it:

- The console sender logged the subject and body only via `extra`, and the development log
  format renders no extra fields — so it printed `email (console)` and nothing else, defeating
  its entire purpose. The detail now goes in the log message itself, with the structured
  fields kept for production's JSON format.
- The doctor's copy of a confirmation read *"your appointment ... with Dr Asha Rao"* — naming
  the doctor to themselves, because both copies shared one template. Each side now names its
  counterpart, with a test asserting the doctor's copy names the patient.

Neither would have been caught by a test asserting only that two messages were queued.
