import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { setUnauthorizedHandler } from '../api/client'
import { auth as authApi } from '../api/endpoints'
import type { User } from '../api/types'
import { AuthContext, type AuthState } from './context'
import { clearSession, loadSession, saveSession } from './session'

export function AuthProvider({ children }: { children: ReactNode }): ReactNode {
  // Read once, synchronously, so the first render already knows whether there is a session to
  // check — and `loading` starts false when there is not, rather than flashing a spinner at
  // every signed-out visitor.
  const [token, setToken] = useState<string | null>(() => loadSession()?.token ?? null)
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState<boolean>(() => loadSession() !== null)

  const signOut = useCallback(() => {
    clearSession()
    setToken(null)
    setUser(null)
    setLoading(false)
  }, [])

  // The 401 handler is registered once and must not be rebuilt when `signOut` changes, or it
  // would race the very request that triggered it.
  const signOutRef = useRef(signOut)
  useEffect(() => {
    signOutRef.current = signOut
  })

  useEffect(() => {
    // One place decides that a rejected token ends the session, rather than every caller
    // remembering to check for a 401.
    setUnauthorizedHandler(() => signOutRef.current())
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    // Mount only. A token restored from storage has to be checked against the API before the
    // app trusts it — the account may have been deactivated, or the signing key rotated.
    // Signing in does *not* go through here: `signIn` already has the user, and re-fetching
    // would be a second identical request on the most latency-sensitive screen there is.
    const stored = loadSession()
    if (stored === null) return

    let cancelled = false
    authApi
      .me(stored.token)
      .then((me) => {
        if (!cancelled) setUser(me)
      })
      .catch(() => {
        if (!cancelled) {
          clearSession()
          setToken(null)
          setUser(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback(async (email: string, password: string): Promise<User> => {
    const granted = await authApi.login({ email, password })
    saveSession(granted.access_token, granted.expires_in)
    const me = await authApi.me(granted.access_token)
    // Set together so no render ever sees a token without the user it belongs to — which is
    // what would send a freshly signed-in doctor to the patient portal for one frame.
    setToken(granted.access_token)
    setUser(me)
    setLoading(false)
    return me
  }, [])

  const value = useMemo<AuthState>(
    () => ({ user, token, loading, signIn, signOut }),
    [user, token, loading, signIn, signOut],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
