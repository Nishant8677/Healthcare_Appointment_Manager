# Frontend — patient, doctor and admin portals

React + TypeScript, built with Vite. Three portals in one application, separated at the
routing layer so the role a screen belongs to is visible in [`src/App.tsx`](src/App.tsx)
rather than buried in a check inside each component.

## Quickstart

The backend must be running first — see the [root README](../README.md).

```bash
npm install
npm run dev
```

Then open <http://localhost:5173>. The port is fixed, because it is what the backend's default
`CORS_ORIGINS` allows: a fresh checkout works with no configuration on either side.

| Script | What it does |
| --- | --- |
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Type-check, then a production bundle in `dist/` |
| `npm run preview` | Serve the production bundle locally |
| `npm run typecheck` | `tsc` with no emit |
| `npm run lint` | oxlint |
| `npm test` | Vitest |

### Configuration

One variable, read at build time:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Where the API lives. |

Set it in `.env.local` for development, or as a build-time variable when deploying:

```bash
VITE_API_BASE_URL=https://your-api-host.example.com npm run build
```

Deliberately **not** a dev-server proxy. The production build is static files served from a
different host than the API, so a proxy would hide the CORS configuration and absolute-URL
handling until deployment day — which is the worst time to find them.

## Three runtime dependencies

`react`, `react-dom`, `react-router-dom`. That is the entire runtime dependency list, and it
is a deliberate answer to the brief's "keep dependencies minimal and native wherever possible".

| The usual choice | What is used instead | Why |
| --- | --- | --- |
| `axios` | `fetch` | The whole surface is JSON over HTTP with a bearer token. [`api/client.ts`](src/api/client.ts) is ~130 lines and puts the care where it belongs: what an error *means*, and what a 401 does to the session. |
| TanStack Query | [`hooks/useResource.ts`](src/hooks/useResource.ts) | ~140 lines covering fetch-on-mount, loading, error, refetch, and not writing state from a request whose screen has gone. Caching and background refetching would be genuinely useful in a larger app; here they would be a dependency earning its keep on one screen out of fifteen. |
| Tailwind / a component library | Plain CSS with custom properties | Three portals need a consistent look and about a dozen components. The tokens live in [`index.css`](src/index.css); no build step, no purge configuration. |
| `zod` + a form library | Native validation + the API's own errors | The backend already validates and writes its messages for a person to read. Duplicating those rules here would mean two places to keep in step — and a client-side rule that silently disagrees with the server. |

The result is **~89 kB gzipped** for the whole application.

## How it is put together

```
src/
  api/          client.ts (the one fetch wrapper), endpoints.ts, types.ts
  auth/         session storage, context, provider, route guards
  components/   shared primitives + the pieces used by more than one portal
  hooks/        useResource / useAction / useNow / useAppointment
  lib/          pure functions: formatting, working-hours conversion
  pages/        patient/  doctor/  admin/  and the public sign-in pages
```

### Things worth knowing

**The token lives in `sessionStorage`, not `localStorage`.** Neither defends against XSS —
both are readable by any script on the origin. The difference is what happens when someone
walks away from a shared consulting-room workstation, which is the normal deployment here.
`sessionStorage` ends the session with the tab and still survives a page refresh. See
[`auth/session.ts`](src/auth/session.ts).

**Route guards are usability, not security.** Every protected endpoint enforces its own roles
server-side. What the guards prevent is a patient being shown a doctor's screen that then
fails piecemeal with 403s — and, more to the point, a doctor's AI triage brief being fetched
into a patient's browser at all.

**Actions report success explicitly.** `useAction(...).run()` returns
`{ok: true, value} | {ok: false}` rather than `Result | undefined`. The obvious shape cannot
tell "it failed" apart from "it succeeded and returned nothing", which is how a booking the
API had already accepted with a `200` silently failed to navigate. Found in a browser, fixed
in the type.

**Dates use the local calendar day, never a sliced ISO string.**
`toISOString().slice(0, 10)` converts to UTC first and so returns *yesterday* for anyone east
of Greenwich for part of every day — invisible in London, constant in Kolkata. There is a test.

## Tests

58 tests, aimed at the places where a bug would be invisible rather than at render output:
error-shape mapping, session expiry, the local-date conversion, working-hours round-tripping,
the action contract, and the role guards.

```bash
npm test
```
