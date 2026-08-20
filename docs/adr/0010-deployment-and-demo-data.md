# ADR 0010 — Deployment and demo data

**Status:** Accepted · **Phase:** 9

Getting the system onto a URL somebody else can open, and putting enough in it that opening it
shows something. Small phase, but two of the decisions are about safety rather than
convenience.

---

## 1. The demo seed goes through the real services

**Decision.** `app/seed.py` creates its appointments by calling `hold_slot`, `confirm_hold`,
`record_visit` and `record_leave` — the same functions the API endpoints call — rather than
inserting rows.

**Why.** A fixture written with direct inserts is a *picture* of a working system. Rows created
by the services are the system's own output: the booking writes its notification outbox
entries, requests its AI summary and queues its calendar sync; the visit produces a real
prescription and exactly fourteen medication reminders; the leave day runs the actual cascade
and sends the actual notice.

That difference matters for a reviewer, who is looking at the queues and the summaries as much
as the appointments. It also means the seed *fails* if the booking rules are broken — a small
extra integration test, run every time anyone sets the project up.

The cost is that seeding has to respect the rules, including "you cannot book a slot in the
past". The completed visit is therefore booked with a `now` five days earlier, which is a
slightly odd line of code with a comment explaining it. Worth it.

## 2. There is no password in the repository

**Decision.** `DEMO_PASSWORD` is required from the environment. The seed refuses to run
without it, and prints a reminder that the value is stored nowhere else.

**Why.** The repository is public. A working credential for a hosted deployment does not
belong in it, even a deliberately disposable one — the deployment is real, reachable, and
holds data that looks like patient records.

This follows `create-admin`, which has taken its password from the environment since Phase 1
for a related reason: a password passed as a command-line flag is visible in shell history and
to anyone who can list processes.

## 3. The seed refuses a database that has accounts in it

**Decision.** If the demo admin exists, the seed reports that it is already seeded and changes
nothing. If *other* accounts exist but the demo admin does not, it refuses and exits non-zero.

**Why.** The failure being prevented is running a demo seed against a live clinic database and
adding a set of shared-password logins to it. That is a mistake you make once, and it is
unrecoverable in the sense that matters: the accounts are real and someone may sign in before
you notice.

An empty database is by definition not a running clinic. Anything else might be, and the guard
costs one query. The idempotent branch exists separately because deploy scripts get re-run,
and "already done" should not be an error.

## 4. One provider, and a two-step deploy that is honest about itself

**Decision.** A single Render blueprint describes the database, the API and the static
portals. `CORS_ORIGINS` and `VITE_API_BASE_URL` are marked `sync: false` and set by hand after
the first deploy.

**Why the single provider.** The conventional split — API on Render, frontend on Vercel — means
two dashboards, two deploy logs and two places to look when something is wrong, in exchange
for a marginally nicer static host. Not worth it for a project whose frontend is a folder of
files.

**Why the manual step.** The two services need each other's URLs, and neither URL exists until
the platform has created them. Render can inject one service's *host* into another's
environment, but not with a scheme attached, and the alternative — teaching the frontend to
prepend `https://` to a bare host — is application code contorted around one deployment
target's syntax.

So the guide says plainly that this is two steps and why. A documented manual step is better
than a clever automatic one that only works on Render, and the failure it causes (the portals
load, every request is blocked by CORS) is called out in the guide because it is the thing
that will actually go wrong.

## 5. Migrations run in the start command

**Decision.** `alembic upgrade head && python run.py`.

**Why.** Render's `preDeployCommand` is a paid feature and this is deployed on free tiers.
Putting migrations in the start command means a schema change that fails takes the deploy down
rather than leaving a running API talking to a database it does not match — which is the right
failure. It would race across multiple instances, but Alembic takes a lock and the free tier
runs one; the guide says so rather than leaving it as a trap.

## 6. Both external integrations are off by default

**Decision.** A fresh deployment has `LLM_PROVIDER=stub` and `EMAIL_PROVIDER=console`, and no
Google credentials.

**Why.** A public URL with a real model key behind it can be run up by a crawler. A public URL
with a real mail provider behind it can send messages to whatever address someone types. The
default is the configuration where neither is possible, and turning each on is one variable
documented in the deployment guide.

The seeded patients use `@example.com`, a reserved domain that accepts nothing — so even with
mail switched on, the demo data cannot reach a real inbox.

## 7. One entry point for development and production

**Decision.** `run.py` reads `HOST` and `PORT` from the environment and decides reload and
bind address from `APP_ENV`. There is no separate production command.

**Why.** The Windows event-loop policy has to be configured before uvicorn creates its loop,
which is why `run.py` exists at all. A second, production-only invocation would be a second
place for that to be forgotten — and it would be forgotten on the machine where nobody
notices, because Linux does not need it. The only real differences between the two
environments are which address to bind and whether to watch the filesystem, and both are
decided by configuration rather than by code.
