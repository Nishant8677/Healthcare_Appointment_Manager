# API

Interactive documentation, generated from the code and always current, is at `/docs` on a
running server. This page is the companion to it: the flows in the order you would actually
perform them, with the error responses that matter.

- **Base URL** — `http://localhost:8000` locally
- **Auth** — `Authorization: Bearer <token>` from `/auth/login`
- **Times** — every timestamp is an instant with an offset (`2026-08-24T09:00:00Z`). Dates in
  query parameters and working hours are the clinic's local calendar, set by `CLINIC_TIMEZONE`.
- **Errors** — `{"detail": "a sentence written for a person"}`, or, for validation,
  `{"detail": [{"loc": ["body", "password"], "msg": "..."}]}`

---

## Authentication

### `POST /auth/register`

Creates a **patient**. The role is never taken from the request — doctor and admin accounts
are created by an admin, so this endpoint cannot be used to obtain elevated access.

```json
{ "email": "meera@example.com", "password": "a-long-enough-password", "full_name": "Meera Iyer" }
```

`201` returns the user. `409` if the address is taken; `422` if the password is under 10
characters.

### `POST /auth/login`

```json
{ "email": "meera@example.com", "password": "a-long-enough-password" }
```

```json
{ "access_token": "eyJhbGciOi...", "token_type": "bearer", "expires_in": 3600 }
```

`401` for an unknown address *or* a wrong password — deliberately one error for both, so the
response cannot be used to discover which addresses are registered with the clinic.

### `GET /auth/me`

The signed-in user. Used by the portals to resolve a stored token on load.

---

## Booking, end to end

The part worth reading carefully. Booking is two steps because a patient needs time to
describe their symptoms without losing the slot, and a slot cannot sit reserved forever for
someone who closed the tab.

### 1. Find a doctor — `GET /doctors?specialisation=Cardiology`

```json
[{ "id": "2154fc32-…", "full_name": "Asha Rao", "specialisation": "Cardiology",
   "slot_duration_minutes": 30, "is_active": true }]
```

### 2. See free times — `GET /doctors/{id}/slots?date=2026-08-24`

Computed live from working hours, slot duration, leave days and existing bookings.

```json
{
  "doctor_id": "2154fc32-…",
  "date": "2026-08-24",
  "slot_duration_minutes": 30,
  "timezone": "UTC",
  "slots": [
    { "starts_at": "2026-08-24T09:00:00Z", "ends_at": "2026-08-24T09:30:00Z" },
    { "starts_at": "2026-08-24T09:30:00Z", "ends_at": "2026-08-24T10:00:00Z" }
  ]
}
```

### 3. Hold it — `POST /appointments/hold`

```json
{ "doctor_id": "2154fc32-…", "starts_at": "2026-08-24T09:00:00Z" }
```

`201` returns the appointment with `status: "held"` and a `hold_expires_at` a few minutes out.
The hold occupies the slot exactly as a confirmed booking does — the reservation is real, not
advisory.

| Status | Meaning |
| --- | --- |
| `409` | The slot was taken between listing and holding, **or** you already hold an unconfirmed slot |
| `422` | Not a valid slot for this doctor: outside working hours, on a leave day, in the past, or past the booking horizon |

### 4. Confirm it — `POST /appointments/{id}/confirm`

```json
{
  "symptoms": "Tightness across my chest when I climb stairs…",
  "duration_days": 12,
  "additional_notes": null
}
```

`200` returns the appointment as `confirmed`. In the same transaction this also queues both
confirmation emails, the patient's reminder, the doctor's pre-visit AI summary, and a calendar
entry for each participant who has connected one.

`410 Gone` if the hold expired — the slot is back in circulation and someone else may already
have it.

### Changing a booking

| Endpoint | Notes |
| --- | --- |
| `POST /appointments/{id}/cancel` | Body `{"reason": "…"}`. Records *who* cancelled: a patient's cancellation and the clinic's are different statuses, because they send different messages |
| `POST /appointments/{id}/reschedule` | Body `{"starts_at": "…"}`. Cancels the old and creates the new in one transaction, so a patient can never hold both or neither. The symptom form comes across; so does the calendar entry, which *moves* rather than disappearing and reappearing |
| `GET /appointments?include_cancelled=false` | Scoped by role in the SQL: a patient's own bookings, a doctor's own schedule, everything for an admin |

---

## The visit

### `POST /appointments/{id}/visit` — doctor or admin

```json
{
  "clinical_notes": "Contact dermatitis on both forearms, consistent with an irritant…",
  "medications": [
    { "drug_name": "Hydrocortisone 1% cream", "dosage": "Thin layer",
      "times_per_day": 2, "duration_days": 7, "instructions": "Apply to affected skin only." }
  ],
  "follow_up_date": "2026-09-07"
}
```

```json
{ "appointment_id": "…", "status": "completed", "completed_at": "…", "reminders_scheduled": 14 }
```

One transaction: the prescription, the completed status, the pending patient summary, and
every medication reminder. `times_per_day` and `duration_days` drive the reminder schedule
directly — never the generated prose. See [llm-prompts.md](llm-prompts.md).

`409` if the appointment is not in a state that can be completed, or if notes are already
filed.

### The summaries

| Endpoint | Who |
| --- | --- |
| `GET /appointments/{id}/pre-visit-summary` | doctor / admin — **never the patient** |
| `GET /appointments/{id}/post-visit-summary` | patient / doctor / admin |

```json
{
  "summary_type": "pre_visit",
  "status": "ready",
  "urgency": "medium",
  "content": {
    "urgency": "medium",
    "chief_complaint": "Exertional chest tightness with breathlessness",
    "suggested_questions": ["…", "…", "…"]
  },
  "prompt_version": "pre-visit-v1",
  "model": "claude-opus-5",
  "attempts": 1,
  "unavailable_reason": null
}
```

A summary that is not ready comes back as `pending` or `failed` with an
`unavailable_reason` — never as `null`. A doctor deciding whether to wait ten seconds or
proceed without it needs to know which.

---

## Administration

All under `/admin`, all admin-only.

| Endpoint | Purpose |
| --- | --- |
| `POST /admin/doctors` | Creates the login and the clinic profile together, optionally with working hours |
| `GET`/`PATCH` `/admin/doctors/{id}` | Read or amend; `is_active: false` withdraws them from patient search |
| `PUT /admin/doctors/{id}/working-hours` | Replaces the whole week |
| `GET /admin/doctors/{id}/leave/impact?date=` | **Read-only.** Whose appointments a leave day would cancel |
| `POST /admin/doctors/{id}/leave` | Records leave; cascades only when acknowledged |
| `DELETE /admin/doctors/{id}/leave/{leave_id}` | Removes a leave day |

### Working hours must divide evenly

```json
{ "working_hours": [{ "weekday": 0, "start_time": "09:00", "end_time": "17:00" }] }
```

`weekday` is `0 = Monday`, matching Python's `date.weekday()`. A window that does not divide
into whole appointments is rejected with a message that tells you what would work:

```json
{ "detail": "Monday 09:00-17:00 is 480 minutes, which is not a whole number of 45-minute appointments. Try ending at 16:30 or 17:15." }
```

### Leave is refused before it cascades

`POST /admin/doctors/{id}/leave` with `{"leave_date": "2026-08-25"}` when patients are booked:

```json
{ "detail": "2 appointment(s) are already booked on 2026-08-25. Review them at /admin/doctors/{id}/leave/impact?date=2026-08-25, then resend with cancel_existing_appointments=true to cancel and notify them." }
```

Look first — `.../leave/impact` lists who is affected by name, time and address, and changes
nothing:

```json
{
  "doctor_id": "…", "leave_date": "2026-08-25", "affected_count": 2,
  "appointments": [
    { "appointment_id": "…", "patient_name": "Meera Iyer",
      "patient_email": "meera@example.com", "starts_at": "…", "status": "confirmed" }
  ]
}
```

Resending with `"cancel_existing_appointments": true` commits the leave day, every
cancellation, every notice and the removal of now-pointless reminders in **one transaction**.

```json
{ "id": "…", "leave_date": "2026-08-25", "reason": "Conference",
  "cancelled_appointments": 2, "patients_notified": 2 }
```

### Watching the queues

| Endpoint | Purpose |
| --- | --- |
| `GET /admin/notifications?status=failed` | The outbox. `failed` means the retry budget is spent |
| `GET /admin/notifications/summary` | Counts pending / sent / failed |
| `POST /admin/notifications/{id}/retry` | Requeue with a full budget, once the cause is fixed |
| `GET /admin/calendar/sync-jobs?status=` | Calendar sync state |
| `GET /admin/calendar/summary` | pending / synced / **skipped** / failed |
| `POST /admin/calendar/sync-jobs/{id}/retry` | Requeue a failed or skipped entry |

`skipped` is not a failure: it means the user has no calendar connected, which is the normal
state for most patients. `failed` is the number worth watching.

---

## Google Calendar

| Endpoint | Notes |
| --- | --- |
| `POST /calendar/connect` | Patients and doctors. Returns the Google consent URL |
| `GET /calendar/callback?code=&state=` | Google's redirect target. Unauthenticated by necessity — the browser arrives with no bearer token — but the `state` is a signed, short-lived token carrying the user id, so it can only ever act for whoever `/connect` issued it to |
| `GET /calendar/connection` | Whether connected, and to which Google account. Never returns a token |
| `DELETE /calendar/connection` | Revokes at Google **and** deletes the stored credential |

`503` from `/calendar/connect` means this deployment has no Google credentials configured — a
supported state, not a fault. Setup: [google-calendar-setup.md](google-calendar-setup.md).

---

## Health

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Never touches the database, so it answers even when Postgres is down |
| `GET /readyz` | Readiness. `503` when the database is unreachable |

Both are public. Every response carries an `X-Request-ID`, echoed from the request if you send
one, and it appears in every log line for that request.
