# Google Calendar setup

The application runs perfectly well **without** any of this. If `GOOGLE_CLIENT_ID` is unset,
calendar sync is inert: nobody can connect a calendar, no sync row is ever written, and
booking, notifications and AI summaries behave exactly as they otherwise would. Set it up when
you want appointments to appear on real calendars.

Fifteen minutes, once. Steps 1–4 are in the Google Cloud Console, step 5 is local
configuration, step 6 is the connection each user makes for themselves.

---

## 1. Create a project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Project picker (top bar) → **New Project**. Name it anything — `healthcare-appointments`
   is fine — and create it.
3. Make sure the picker now shows the new project before continuing. Enabling an API on the
   wrong project is the most common way to lose twenty minutes here.

## 2. Enable the Calendar API

**APIs & Services → Library** → search *Google Calendar API* → **Enable**.

Nothing else needs enabling. The app does not use Gmail, Drive, or People.

## 3. Configure the consent screen

**APIs & Services → OAuth consent screen**.

| Field | Value |
|---|---|
| User type | **External** (unless you have a Workspace organisation, in which case Internal is simpler) |
| App name | Healthcare Appointment Manager |
| User support email | your address |
| Developer contact | your address |

On the **Scopes** step, add:

```
https://www.googleapis.com/auth/calendar.events
```

That is the narrowest scope that permits creating, updating and deleting events. Do **not**
add `https://www.googleapis.com/auth/calendar` — it grants control over the user's entire
calendar list, including deleting calendars, which this application has no use for.

`openid` and `email` are added automatically and are used only to record which Google account
was connected, so the portal can show the user which calendar their appointments are going to.

On the **Test users** step, add every Google account you intend to connect. While the app is
in *Testing* mode Google refuses consent for anyone not on that list, with an error that does
not explain why.

> **Refresh tokens expire after 7 days while the app is in Testing mode.** This is a Google
> policy, not a bug in this application: a connection made today stops working next week and
> the user has to reconnect. The app handles it correctly — Google answers `invalid_grant`,
> the connection is marked revoked, the pending sync rows are recorded as skipped, and the
> portal asks the user to reconnect. Publishing the app removes the limit.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create Credentials → OAuth client ID**.

- **Application type:** Web application
- **Authorised redirect URIs:** add the callback for every environment you will use:

  ```
  http://localhost:8000/calendar/callback
  https://your-api-host.example.com/calendar/callback
  ```

  This must match `GOOGLE_REDIRECT_URI` **character for character** — trailing slash, scheme
  and port included. A mismatch produces `Error 400: redirect_uri_mismatch`, and the message
  does not tell you which of the two is wrong.

Copy the **Client ID** and **Client secret**.

## 5. Configure the backend

Generate an encryption key for the stored refresh tokens:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then in `backend/.env`:

```dotenv
GOOGLE_CLIENT_ID=1234567890-abcdef.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_REDIRECT_URI=http://localhost:8000/calendar/callback
CALENDAR_TOKEN_KEY=<the key you just generated>
```

Three things about that key:

- It is **required** whenever `GOOGLE_CLIENT_ID` is set. Startup fails with an explicit error
  otherwise, rather than storing calendar credentials in plaintext.
- **Losing it invalidates every stored connection.** Users would have to reconnect. Keep it
  with your other production secrets.
- **Changing it does not silently break things.** Tokens that cannot be decrypted mark the
  connection revoked with a reason, so the portal says "reconnect" rather than doing nothing.

Restart the API. The calendar worker starts alongside the notification and summary workers.

## 6. Connect a calendar

Each user connects their own — patients and doctors alike. Nothing is written to anyone's
calendar without them going through this.

```bash
# 1. Sign in and get the consent URL
curl -s -X POST http://localhost:8000/calendar/connect \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

Open the returned `authorization_url` in a browser, approve, and Google redirects to the
callback. With no `CALENDAR_RETURN_URL` set you get JSON back:

```json
{ "connected": true, "google_account_email": "you@gmail.com", "appointments_queued": 2 }
```

`appointments_queued` is the backfill: existing upcoming appointments are queued on connect,
so the feature does something visible immediately instead of appearing to do nothing until the
next booking.

Check the state at any time:

```bash
curl -s http://localhost:8000/calendar/connection -H "Authorization: Bearer $TOKEN"
```

Disconnect — this revokes the grant at Google *and* deletes the stored token:

```bash
curl -s -X DELETE http://localhost:8000/calendar/connection -H "Authorization: Bearer $TOKEN"
```

---

## What happens to a calendar entry

| Event in the app | Effect on Google |
|---|---|
| Patient confirms a booking | An event is created on each connected participant's calendar |
| Patient reschedules | The **same** event moves to the new time — the entry does not disappear and reappear |
| Patient or doctor cancels | The event is deleted |
| Admin marks the doctor on leave | Every affected event on both sides is deleted |
| User connects a calendar | Their upcoming appointments are queued and appear shortly after |
| User revokes access at Google | The connection is marked revoked; pending entries are recorded as skipped, not failed |

The two parties get **separate events on their own calendars**, rather than one event with the
other as an attendee. An invitation would put a second entry on the recipient's calendar
alongside the one written directly, and send a Google invitation email competing with the
clinic's own confirmation.

Events carry the time, the other party's name, and the specialisation. They deliberately do
**not** carry the symptom report: a calendar entry syncs to phones and lock screens, which is
the wrong place for a description of somebody's medical complaint.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Error 400: redirect_uri_mismatch` | `GOOGLE_REDIRECT_URI` differs from the registered URI. Compare them character by character. |
| `Error 403: access_denied` during consent | The Google account is not in the **Test users** list on the consent screen. |
| `502 … Google returned no refresh token` | A stale grant exists. Remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and connect again. |
| `503 Google Calendar is not configured` | `GOOGLE_CLIENT_ID` is unset. Expected if you are running without calendar sync. |
| Connection worked, then stopped after a week | Testing-mode refresh tokens expire after 7 days. Reconnect, or publish the app. |
| Entries show as `skipped` in the admin view | The user has no connected calendar. Not an error. Requeue them with the retry endpoint once they connect. |

The admin view is the place to look first:

```bash
curl -s http://localhost:8000/admin/calendar/summary -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s "http://localhost:8000/admin/calendar/sync-jobs?status=failed" -H "Authorization: Bearer $ADMIN_TOKEN"
```

`skipped` is expected and healthy — most patients never connect a calendar. `failed` is the
number worth watching, and each failed row carries the reason Google gave.

---

## Why there is no `google-api-python-client`

The official client libraries pull in roughly ten transitive packages — `google-api-core`,
`protobuf`, `grpcio` and their dependencies — to wrap four HTTP requests: the consent
redirect, the code exchange, the token refresh, and the event write. This project talks to the
REST API directly with the `httpx` it already depends on, in
[`google_oauth.py`](../backend/src/app/services/google_oauth.py) and
[`google_calendar.py`](../backend/src/app/services/google_calendar.py).

The one dependency added for this feature is `cryptography`, and it is there for a specific
reason: an OAuth refresh token is working access to somebody's calendar, so it is encrypted
before it reaches a database column. The alternative to a vetted crypto library is hand-rolled
cipher code, which is the one case where writing it yourself is the unsafe option.
