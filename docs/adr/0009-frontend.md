# ADR 0009 — The three portals

**Status:** Accepted · **Phase:** 8

Patient, doctor and admin portals over the existing API. The API was built first and does not
change in this phase, which makes the decisions here almost entirely about two things: what
the frontend is allowed to depend on, and where the boundaries between the three portals live.

---

## 1. Three runtime dependencies

**Decision.** `react`, `react-dom`, `react-router-dom`. The HTTP client, the data-fetching
layer, the styling and the form handling are written here.

**Why.** The brief asks for minimal dependencies, and the conventional React starting point —
axios, TanStack Query, Tailwind, zod, a form library, a component library — is six or seven
packages before a line of application code. Each was weighed rather than dropped by reflex:

- **`fetch` over axios.** The entire surface is JSON over HTTP with a bearer token. The
  interesting parts are what an error *means* and what a 401 does to the session, and both are
  clearer written out than configured through interceptors.
- **A hook over TanStack Query.** What is actually needed is fetch-on-mount, loading, error,
  refetch after a mutation, and not writing state from a request whose screen has gone. That
  is `useResource`. Caching, background refetching and query invalidation are real features —
  they would earn their place in a larger app, and on one screen out of fifteen here they
  would not.
- **CSS custom properties over Tailwind.** Three portals, one visual language, about a dozen
  components. No build step and no purge configuration to get wrong.
- **The API's validation over a client schema.** The backend already validates and writes its
  messages for a person to read ("Monday 09:00-17:00 is 480 minutes, which is not a whole
  number of 45-minute appointments. Try ending at 16:30 or 17:15."). Restating those rules
  here would mean two places to keep in step, and a client-side rule that quietly disagrees
  with the server is worse than no client-side rule at all.

`react-router-dom` is kept because routing with guards is where a hand-rolled version quietly
lacks things, and the role separation below depends on it.

The whole application is ~89 kB gzipped.

## 2. Portals are separated by route, not by convention

**Decision.** Each portal's screens sit under a `RequireRole` route in one place in `App.tsx`.

**Why.** The alternative — every component checking the role at the top — puts the security
model in fifteen files and makes "which portal is this screen in" unanswerable without reading
all of them. Here, adding a screen to the wrong portal is a change you can see in a diff.

**This is a usability boundary, not a security one.** Every protected endpoint enforces its
own roles server-side, and the guards would be worthless if they did not. What they prevent is
a patient being shown a doctor's screen that then fails piecemeal with 403s — and, more
concretely, the doctor's AI triage brief being fetched into a patient's browser at all. The
patient's own appointment page never requests it; there is no route from which it could.

## 3. The token lives in `sessionStorage`

**Decision.** `sessionStorage`, with the expiry recorded alongside it and a minute of margin
so a token that would die mid-request is discarded before it is sent.

**Why.** Neither web storage defends against XSS — both are readable by any script on the
origin — so the choice is not about that. It is about what happens when someone walks away.
A shared reception desk or consulting-room workstation is the normal deployment for this
software, and `localStorage` would leave a doctor signed in for whoever sits down next.
`sessionStorage` ends the session with the tab and still survives a page refresh, so it costs
nothing day to day.

The properly secure answer is a short-lived token in memory plus an httpOnly refresh cookie,
which the backend does not issue — ADR 0002 keeps auth stateless. Choosing the safer of the
two available options, and saying plainly why it is not the best one, is the honest position.

## 4. Failure states are designed, not defaulted

**Decision.** `DataState` renders one of four things for every resource: spinner, error,
empty, or content. `SummaryPanel` renders one of three for an AI summary: pending, failed with
a reason, or the text.

**Why.** The empty and error cases are the ones that get skipped when each screen writes its
own, and this application has an unusual number of states that are *not* failures and must not
look like them:

- An AI summary that is `pending` is normal — the model is asked outside the request.
- A calendar entry that is `skipped` means the user has no calendar connected, which is true
  of most patients.
- A calendar integration that is not configured at all is a supported deployment, so the
  settings page says "everything else works normally" rather than showing an error.

Each of those needs different words, and a generic "something went wrong" would misrepresent
all three.

## 5. The slot hold is shown as a countdown

**Decision.** The symptom form displays the time left on the hold, ticking every second, and
turns red under a minute. When it reaches zero the form is replaced by an explanation.

**Why.** The hold is enforced by the API whatever the screen does. But a form that simply
fails on submit — after the patient has typed out their symptoms — is a bad experience for a
mechanism that exists to be fair to *other* patients. Making the deadline visible turns an
invisible rule into an obvious one, and it means the reviewer can see the hold-then-confirm
design working rather than having to take the README's word for it.

## 6. The leave cascade shows its cost before it is paid

**Decision.** Choosing a date on the admin's leave form loads the read-only impact preview
immediately, lists each affected patient by name, time and email, and relabels the button to
"Cancel N appointments and record leave" in a destructive style.

**Why.** The backend already refuses to cascade without explicit acknowledgement (ADR 0006).
The UI's job is to make that acknowledgement *informed* rather than a checkbox someone ticks
to make an error go away. Cancelling several people's medical appointments should never be a
side effect of picking a date, and an admin who can see the three names is far less likely to
proceed by accident.

## 7. Two bugs the browser found that tests did not

Worth recording, because both are the kind that unit tests of pure functions cannot reach.

**The action contract was ambiguous.** `useAction(...).run()` returned `Result | undefined`,
using `undefined` to mean failure. An action whose whole job is a side effect returns nothing
on success — indistinguishable from failure. Three call sites guarded on it; the symptom form
therefore stopped navigating after a booking the API had already accepted with a `200`. The
fix is a discriminated result, `{ok: true, value} | {ok: false}`, which makes the mistake
unrepresentable rather than merely fixed. Tests now pin it.

**Names were prefixed blindly.** The seeded records store "Dr Asha Rao", and every screen
prefixed another title: "Dr Dr Asha Rao". Admins type names both ways and always will, so the
prefix is now conditional on the name not already carrying an honorific — with a test that
"Drew Patel" is not mistaken for one.

Neither was visible in the type system or in a passing test suite. Both took thirty seconds to
find by using the application.
