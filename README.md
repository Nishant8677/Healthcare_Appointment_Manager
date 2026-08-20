# Healthcare Appointment & Follow-up Manager

A clinic platform with separate patient, doctor and admin portals. Patients book appointments
and describe their symptoms up front; the doctor gets an AI-generated pre-visit summary with an
urgency level; after the visit the patient gets a plain-language summary with a medication
schedule. Both sides stay informed through email and Google Calendar.

> **Status: Phase 0 complete** — project foundation, configuration, database layer, health
> probes and the test harness are in place. Feature phases are tracked in
> [Roadmap](#roadmap) below.

---

## Why this is built the way it is

The hard parts of this system are not the CRUD screens; they are the failure modes:

| Problem | Approach |
| --- | --- |
| Two patients booking the same slot at once | Postgres row locks inside the booking transaction, backed by a partial unique index as the final guarantee |
| A patient losing their slot while filling the symptom form | Short-lived `HELD` reservation with a TTL, checked at confirmation time |
| A doctor going on leave over existing bookings | Cascade that cancels, notifies every affected patient and cleans up calendar events |
| Email or calendar provider being down | Transactional outbox: notifications are rows committed with the booking, delivered by a worker with capped exponential backoff |
| The LLM being slow, down, or returning malformed output | Summaries are generated out of band and schema-validated; a booking never fails because the model did |

Each of these is covered in the design write-up (Phase 9) as it is implemented.

---

## Architecture

```
                 ┌────────────────────┐
  Browser  ────► │  React (Vite) SPA  │
                 │  patient / doctor  │
                 │  / admin portals   │
                 └─────────┬──────────┘
                           │ REST + JWT
                 ┌─────────▼──────────┐        ┌──────────────────┐
                 │   FastAPI backend  │───────►│  LLM provider    │
                 │                    │        └──────────────────┘
                 │  api/    routers   │        ┌──────────────────┐
                 │  services/ domain  │───────►│  Email provider  │
                 │  models/  ORM      │        └──────────────────┘
                 │  workers/ jobs     │        ┌──────────────────┐
                 └─────────┬──────────┘───────►│  Google Calendar │
                           │                   └──────────────────┘
                 ┌─────────▼──────────┐
                 │    PostgreSQL      │
                 │  appointments,     │
                 │  notification_jobs │
                 └────────────────────┘
```

Business logic lives in `services/` as plain functions over a session; routers in `api/` handle
HTTP concerns only, and `workers/` drives everything asynchronous. External calls (LLM, email,
calendar) sit behind service interfaces so they can be substituted in tests without network
access.

**Stack:** FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · Alembic · React (Vite) ·
APScheduler. Dependencies are kept deliberately minimal — no message broker or task queue,
because a polled jobs table meets the reliability requirement at this scale with far less
operational surface.

---

## Quickstart

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker (for the local
database), Node 20+ (frontend, from Phase 8).

```bash
git clone https://github.com/<your-username>/Healthcare_Appointment_Manager.git
cd Healthcare_Appointment_Manager
```

Start Postgres (creates both the development and test databases):

```bash
docker compose up -d
```

Configure and install the backend:

```bash
cp .env.example backend/.env
```

Generate a real signing secret and paste it into `backend/.env` as `JWT_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```bash
cd backend && uv sync
```

Apply migrations and run the API:

```bash
uv run alembic upgrade head
```

```bash
uv run uvicorn app.main:create_app --factory --reload
```

The API is then at <http://localhost:8000>, with interactive documentation at
<http://localhost:8000/docs>.

### Verify it is working

```bash
curl http://localhost:8000/healthz
```

```bash
curl http://localhost:8000/readyz
```

`/healthz` is liveness only and never touches the database. `/readyz` reports database
connectivity and returns `503` when it is unreachable, so a database blip degrades the service
instead of killing the container.

---

## Configuration

Every variable the backend reads. See [`.env.example`](.env.example) for a copyable template.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APP_ENV` | no | `dev` | `dev` \| `test` \| `prod`. Controls JSON logging and fail-fast behaviour on startup. |
| `LOG_LEVEL` | no | `INFO` | Root log level. |
| `DATABASE_URL` | **yes** | — | Postgres URL. `postgres://` and `postgresql://` prefixes are rewritten to the async driver automatically. |
| `TEST_DATABASE_URL` | no | local test DB | Database used by the test suite. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no | `10` | Connection timeout. Without it an unreachable database hangs the request instead of failing. |
| `JWT_SECRET` | **yes** | — | Access-token signing key; must be at least 32 characters. |
| `JWT_ALGORITHM` | no | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_TTL_MINUTES` | no | `60` | Access-token lifetime. |
| `CORS_ORIGINS` | no | `http://localhost:5173` | Comma-separated allowed frontend origins. |

Variables for email, the LLM provider and Google Calendar are added in their respective phases
and documented here as they land.

---

## Development

Run from the `backend/` directory:

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format .
```

```bash
uv run mypy
```

Install the pre-commit hooks once, from the repository root, to run linting and a
committed-secret check automatically:

```bash
pre-commit install
```

Tests run against a real PostgreSQL database rather than SQLite. The concurrency guarantees
this project is judged on depend on Postgres row locking and partial unique indexes, so a
substitute engine would let genuinely broken booking code pass.

---

## Project structure

```
backend/
  alembic/            database migrations
  src/app/
    api/              HTTP routers (thin; no business logic)
    core/             settings, database, logging, middleware
    models/           SQLAlchemy models
    schemas/          pydantic request/response contracts
    services/         domain logic
    workers/          background jobs
  tests/
docs/                 ER diagram, architecture notes, design write-up
scripts/              database bootstrap
frontend/             React SPA (Phase 8)
```

---

## Roadmap

- [x] **Phase 0** — Foundation: configuration, async database layer, structured logging with
      request correlation, health probes, migrations, lint/type/test tooling.
- [ ] **Phase 1** — Authentication and the full data model; role-based access for
      patient / doctor / admin.
- [ ] **Phase 2** — Admin doctor management: specialisation, working hours, slot duration, leave.
- [ ] **Phase 3** — Availability and booking: slot generation, hold-then-confirm, double-booking
      prevention with a concurrent-request test.
- [ ] **Phase 4** — Notification outbox and email delivery with capped retries.
- [ ] **Phase 5** — Doctor leave conflict cascade.
- [ ] **Phase 6** — LLM pre-visit and post-visit summaries with graceful degradation;
      medication reminders.
- [ ] **Phase 7** — Google Calendar OAuth and event lifecycle.
- [ ] **Phase 8** — React portals.
- [ ] **Phase 9** — Deployment, seed data, API documentation and the design write-up.

---

## License

MIT
