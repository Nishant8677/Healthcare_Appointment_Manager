/** Where the access token lives between page loads.
 *
 * `sessionStorage`, not `localStorage`. Both are readable by any script on the origin, so
 * neither defends against XSS — the difference is what happens when someone walks away.
 * A clinic reception desk or a shared consulting-room workstation is the normal deployment
 * here, and `localStorage` would leave a doctor signed in for the next person to sit down.
 * `sessionStorage` ends the session with the tab, which is the behaviour a shared machine
 * needs. It still survives a page refresh, so it costs nothing in day-to-day use.
 *
 * The properly secure answer is a short-lived token in memory plus an httpOnly refresh cookie,
 * which the backend does not issue (ADR 0002 keeps auth stateless). Choosing the safer of the
 * two available options, and saying why, is the honest position.
 */

const STORAGE_KEY = 'ham.session'

export interface StoredSession {
  token: string
  /** Epoch milliseconds. Derived from the login response's `expires_in`. */
  expiresAt: number
}

/** A minute of slack, so a token that dies mid-request is discarded before it is sent. */
const EXPIRY_MARGIN_MS = 60_000

function storage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    // Private browsing modes and some embedded webviews throw on access rather than on write.
    return null
  }
}

export function isUsable(session: StoredSession | null, now: number = Date.now()): boolean {
  return session !== null && session.expiresAt - EXPIRY_MARGIN_MS > now
}

export function loadSession(now: number = Date.now()): StoredSession | null {
  const raw = storage()?.getItem(STORAGE_KEY)
  if (!raw) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    // Corrupt or hand-edited: treat as signed out rather than crashing the whole app on boot.
    clearSession()
    return null
  }

  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    typeof (parsed as StoredSession).token !== 'string' ||
    typeof (parsed as StoredSession).expiresAt !== 'number'
  ) {
    clearSession()
    return null
  }

  const session = parsed as StoredSession
  if (!isUsable(session, now)) {
    clearSession()
    return null
  }
  return session
}

export function saveSession(token: string, expiresInSeconds: number): StoredSession {
  const session: StoredSession = { token, expiresAt: Date.now() + expiresInSeconds * 1000 }
  storage()?.setItem(STORAGE_KEY, JSON.stringify(session))
  return session
}

export function clearSession(): void {
  storage()?.removeItem(STORAGE_KEY)
}
