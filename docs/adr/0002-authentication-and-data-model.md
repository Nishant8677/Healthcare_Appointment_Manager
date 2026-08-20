# ADR 0002 — Authentication and the data model

**Status:** Accepted · **Phase:** 1

The schema itself is documented in [data-model.md](../data-model.md); this records the
decisions behind it and behind authentication.

---

## 1. The whole schema is defined in one migration, not grown per phase

**Decision.** All ten tables — including appointments, summaries, prescriptions and the
notification outbox, none of which have logic yet — were created in Phase 1.

**Why.** The relationships between them are what make the design coherent, and they are
easier to get right when designed together than when bolted on one at a time. It also means
one ER diagram describes the finished system, and later phases add only behaviour. The cost
is tables that sit empty for a few phases; the benefit is that no later phase needs a
migration that reshapes something already carrying data.

## 2. Native PostgreSQL enums, with the downgrade fixed by hand

**Decision.** Statuses and roles are native `CREATE TYPE ... AS ENUM` types, storing the
lower-case member *values* rather than SQLAlchemy's default of member names.

**Why.** The database rejects an invalid status outright, and the stored value is identical
to what the API emits, so a row can be read without a translation step.

The catch, found by round-tripping the migration: `op.drop_table` does **not** drop the enum
types it implicitly created, so a downgrade left all seven types behind and the next upgrade
failed with `type "user_role" already exists`. The downgrade therefore drops them
explicitly. Autogenerate does not write this, and it would not have been noticed without
actually running `upgrade → downgrade → upgrade`.

## 3. One `users` table for all three roles

**Decision.** Patients, doctors and admins share a table, distinguished by `role`, with
doctor-specific data in `doctor_profiles`.

**Why.** Everything authentication does — password verification, deactivation, addressing a
notification — is identical across roles. Three tables would triple that logic and make
"find the user for this email" a three-way search. Role-specific columns that are null for
two thirds of rows are the alternative worth avoiding.

## 4. Argon2id for passwords

**Decision.** `argon2-cffi` at its default parameters, rather than bcrypt.

**Why.** Argon2id is memory-hard, which raises the cost of GPU-based cracking, and it is the
current OWASP first choice. It also avoids bcrypt's silent truncation of anything past 72
bytes — a trap where a long passphrase is quietly weakened. `verify_password` treats a
malformed stored hash as a failed login rather than raising, so a corrupt row cannot turn
the login route into a 500.

## 5. Stateless JWT access tokens, no refresh token

**Decision.** A signed access token carrying `sub`, `role`, `type`, `exp`, `iat` and `jti`.
No refresh token, no server-side session store.

**Why.** The assignment needs role-based access across three portals, not a long-lived
session system, and a token store would add a table and a revocation path for no graded
benefit. Two details keep the door open: `type` distinguishes access tokens so a refresh
token could never be replayed as one, and `jti` gives each token an identity so individual
revocation can be added later without invalidating everyone's tokens.

The accepted trade-off is that a token stays valid until it expires. It is bounded by
`ACCESS_TOKEN_TTL_MINUTES`, and because the current user is loaded from the database on
every request, deactivating an account takes effect immediately even though the token itself
is still cryptographically valid.

## 6. Roles are enforced by a dependency, not by an `if` in the handler

**Decision.** `Depends(require_roles(UserRole.ADMIN))` in the route signature.

**Why.** The permission rule becomes part of the route's declaration, so it appears in the
generated API documentation and is visible at a glance during review. A check buried at the
top of a handler is easy to omit when adding the next endpoint, and its absence looks
exactly like ordinary code.

## 7. Registration always creates a patient

**Decision.** The role is hard-coded in the register route and never read from the request
body. Doctors and admins are created by an admin.

**Why.** Trusting a client-supplied role would let anyone register as an administrator of a
medical system. There is a test asserting that a payload containing `"role": "admin"` still
produces a patient.

## 8. Login cannot be used to discover who is a patient

**Decision.** Unknown email and wrong password return the same status and the same message.
When the address is unknown, a password hash is computed anyway before failing.

**Why.** Different responses would turn the login endpoint into a membership oracle for the
clinic — itself sensitive medical information. The throwaway hash keeps the timing of the
two paths comparable, closing the side channel that a fast "no such user" would open.

## 9. Tests migrate a real database and truncate between cases

**Decision.** The suite runs `alembic downgrade base` then `upgrade head` once per session
against `healthcare_test`, and truncates every table before each test.

**Why.** Running the real migrations means the suite proves they work — a schema that exists
only in the models would deploy to nothing. Truncating *before* each test rather than after
means a test that fails midway cannot poison the next one.

This surfaced a real defect: Alembic's generated `env.py` calls `fileConfig()` with
`disable_existing_loggers` left at its default of `True`, which switched off every
application logger for the rest of the session once migrations ran in-process. The
request-correlation regression test caught it. `env.py` now passes
`disable_existing_loggers=False`.
