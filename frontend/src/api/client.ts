/** The HTTP boundary: one `request` function that every API call goes through.
 *
 * `fetch`, not a client library. The whole surface is JSON over HTTP with a bearer token, and
 * a library would add a dependency to re-implement what the platform already does — while
 * hiding the two things that actually need care here: what an error *means*, and what happens
 * to a session when the token stops working.
 */

/** Where the API lives. Baked in at build time so a static deploy can point anywhere. */
export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

/** Called when the API rejects a token, so the app can clear the session exactly once. */
type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
}

export class ApiError extends Error {
  readonly status: number
  /** Field-level messages from a 422, keyed by field name. */
  readonly fieldErrors: Record<string, string>

  constructor(status: number, message: string, fieldErrors: Record<string, string> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
  }

  /** True when the slot was taken, the hold lapsed, or the state moved on. */
  get isConflict(): boolean {
    return this.status === 409 || this.status === 410
  }
}

/** FastAPI answers with `{detail: string}` or, for validation, `{detail: [{loc, msg}, …]}`. */
interface ErrorBody {
  detail?: string | Array<{ loc?: unknown[]; msg?: string }>
}

function describe(status: number, body: ErrorBody | null): [string, Record<string, string>] {
  const detail = body?.detail

  if (typeof detail === 'string') return [detail, {}]

  if (Array.isArray(detail)) {
    // A 422 from pydantic. `loc` is like ["body", "password"]; the last element names the
    // field, which is what a form needs in order to show the message beside the right input.
    const fields: Record<string, string> = {}
    for (const item of detail) {
      const path = Array.isArray(item.loc) ? item.loc : []
      const field = path.length ? String(path[path.length - 1]) : '_'
      if (item.msg && !fields[field]) fields[field] = item.msg
    }
    const first = Object.values(fields)[0]
    return [first ?? 'Some of the details are not valid.', fields]
  }

  // No usable body: say what the status means rather than showing a bare number.
  if (status === 401) return ['Your session has expired. Please sign in again.', {}]
  if (status === 403) return ['Your account does not have access to this.', {}]
  if (status === 404) return ['That could not be found.', {}]
  if (status >= 500) return ['The server had a problem. Please try again shortly.', {}]
  return [`Request failed (${status}).`, {}]
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  token?: string | null
  query?: Record<string, string | number | boolean | undefined>
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, token, query, signal } = options

  const url = new URL(`${API_BASE_URL}${path}`)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) url.searchParams.set(key, String(value))
  }

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    // An aborted request is the caller changing their mind, not a failure to report.
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'Could not reach the server. Check your connection and try again.')
  }

  if (response.status === 204) return undefined as T

  let payload: unknown = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      // Signalled before throwing so the session is cleared even if the caller only renders
      // the message. Otherwise the app would keep showing "expired" on every subsequent
      // action while still believing it is signed in.
      onUnauthorized?.()
    }
    const [message, fieldErrors] = describe(response.status, payload as ErrorBody | null)
    throw new ApiError(response.status, message, fieldErrors)
  }

  return payload as T
}
