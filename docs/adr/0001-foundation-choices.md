# ADR 0001 — Foundation choices

**Status:** Accepted · **Phase:** 0

Decisions made while laying the foundation, recorded because each one is load-bearing for a
requirement that arrives in a later phase.

---

## 1. PostgreSQL, and tests run against it rather than SQLite

**Decision.** PostgreSQL 16 is the only supported database. The test suite connects to a real
`healthcare_test` database; SQLite is not used even for unit tests.

**Why.** Double-booking prevention (Phase 3) rests on two Postgres-specific mechanisms:
`SELECT … FOR UPDATE` row locking and a *partial* unique index that constrains only rows in
active states. SQLite supports neither faithfully. A suite running on SQLite would happily
pass while the booking code was genuinely racy — the exact defect this project is judged on.
Requiring Docker locally is a smaller cost than a test suite that cannot fail correctly.

## 2. Async SQLAlchemy with psycopg 3

**Decision.** `postgresql+psycopg://` on SQLAlchemy 2's async API, one driver for both the
application and Alembic.

**Why.** The workload is I/O-bound almost everywhere that matters: LLM calls, email delivery
and Google Calendar requests all block on the network. A single driver that serves both sync
(migrations) and async (requests) avoids carrying asyncpg *and* psycopg for the same job.

## 3. Database URLs are normalised at load time

**Decision.** `postgres://` and `postgresql://` are rewritten to `postgresql+psycopg://` in
settings validation.

**Why.** Managed providers (Neon, Render, Railway) hand out URLs without a driver prefix.
Pasting one into a deployment dashboard and getting an unrelated-looking SQLAlchemy error is a
predictable deploy-day failure. Normalising once, centrally, removes the whole class.

## 4. Liveness and readiness are separate endpoints

**Decision.** `/healthz` never touches the database. `/readyz` checks it and returns `503`
when it is unreachable.

**Why.** If the liveness probe touched the database, a transient database outage would make
the platform conclude the container was dead and restart it — turning a recoverable
dependency blip into a restart loop. Splitting the probes lets an instance report "alive but
not ready" and recover on its own.

## 5. Startup fails loudly in development, degrades in production

**Decision.** An unreachable database aborts startup when `APP_ENV != prod`; in production it
logs the failure and continues serving, with `/readyz` reporting the degraded state.

**Why.** A developer running against a stopped database should be told immediately rather than
debugging confusing request failures. A production instance that refuses to boot during a
database restart cannot come back on its own once the dependency returns.

## 6. The session dependency does not auto-commit

**Decision.** `get_session` yields a session and guarantees rollback and close, but never
commits. Services open and commit their own transactions.

**Why.** Booking and the leave-cancellation cascade depend on precise transaction boundaries —
a row lock must be held across a check-then-write, and notification rows must commit in the
*same* transaction as the appointment for the outbox pattern to be safe. An implicit
commit-on-success at the framework layer would obscure exactly the boundaries that need to be
explicit and reviewable.

## 7. UUID primary keys

**Decision.** UUIDs generated database-side via `gen_random_uuid()`, not auto-increment
integers.

**Why.** Appointment and prescription identifiers appear in URLs, email links and calendar
payloads. Sequential integers would let one patient enumerate another patient's records by
editing a URL — an avoidable exposure of medical data.

## 8. Constraint names are pinned by a naming convention

**Decision.** `Base.metadata` carries an explicit naming convention for indexes, unique
constraints, checks, foreign keys and primary keys.

**Why.** Alembic can only drop or alter a constraint it can name. Without a convention, the
partial unique index added in Phase 3 gets an auto-generated name that a later migration would
have to guess at.

## 9. No message broker

**Decision.** Background work uses a polled `notification_jobs` table driven by APScheduler
inside the API process, rather than Celery with Redis or RabbitMQ.

**Why.** The reliability requirement is that a notification is never lost when a provider is
down. A transactional outbox delivers that *better* than a broker, because the job row commits
atomically with the booking that caused it — a broker enqueue can succeed while the
transaction rolls back, or vice versa. It also keeps the free-tier deployment to one service
and one database, and keeps the dependency list within the submission guidelines.

## 10. Event-loop policy is set at the entry point, not in application code

**Decision.** `configure_event_loop_policy()` selects the selector event loop on Windows and is
called from `run.py`, Alembic's `env.py` and the test suite's `conftest.py` — never from
application modules.

**Why.** psycopg's async driver raises `InterfaceError` on Windows' default `ProactorEventLoop`,
so local development and the test suite cannot connect at all without this. It has to run
before any loop is created: uvicorn imports the application *inside* its already-running loop,
so an import-time fix in `app/` would come too late. Linux deployment is unaffected, which is
precisely why it belongs at the entry point rather than in the runtime path.

## 11. Every database connection has an explicit timeout

**Decision.** `connect_timeout` is always passed to the engine, configurable via
`DB_CONNECT_TIMEOUT_SECONDS`.

**Why.** Observed directly during Phase 0: with no database listening, a connection attempt did
not receive a prompt refusal — it hung indefinitely, and the test suite stalled rather than
failing. A request thread blocked forever on an unreachable database is worse than one that
returns an error, because it exhausts the pool and takes healthy endpoints down with it. With
the timeout the same condition surfaces in seconds as `connection timeout expired`.

## 12. Structured logging with request correlation, stdlib only

**Decision.** Standard-library `logging` with a JSON formatter in production and a contextvar
carrying a per-request id, rather than a logging library.

**Why.** Debugging a failed notification means following one booking across an HTTP request,
a background job and an external API call. Correlation ids make that possible; a dependency
is not needed to get them, and the submission guidelines ask for a minimal dependency set.
