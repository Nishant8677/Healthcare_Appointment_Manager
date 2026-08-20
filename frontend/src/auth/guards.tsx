import type { ReactNode } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import type { UserRole } from '../api/types'
import { Spinner } from '../components/ui'
import { HOME_FOR_ROLE } from './context'
import { useAuth } from './useAuth'

/** Gate a route on being signed in with one of `allow`.
 *
 * This is a usability boundary, not a security one — the token is what the API checks, and
 * every protected endpoint enforces its own roles. What this prevents is a patient being shown
 * a doctor's screen that then fails piecemeal with 403s, and a doctor's triage brief appearing
 * in a patient's browser at all.
 */
export function RequireRole({ allow }: { allow: readonly UserRole[] }): ReactNode {
  const { user, loading } = useAuth()
  const location = useLocation()

  // The stored token has not been checked yet. Rendering the login page here would bounce
  // anyone who refreshes a deep link straight out of the app.
  if (loading) return <Spinner label="Checking your session" />

  if (user === null) {
    // `state.from` so signing in returns to where they were headed.
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  if (!allow.includes(user.role)) {
    return <Navigate to={HOME_FOR_ROLE[user.role]} replace />
  }

  return <Outlet />
}

/** Send a signed-in user away from the sign-in and registration pages. */
export function RedirectIfSignedIn({ children }: { children: ReactNode }): ReactNode {
  const { user, loading } = useAuth()
  if (loading) return <Spinner label="Checking your session" />
  if (user !== null) return <Navigate to={HOME_FOR_ROLE[user.role]} replace />
  return children
}

/** The bare `/` route: wherever this person belongs. */
export function HomeRedirect(): ReactNode {
  const { user, loading } = useAuth()
  if (loading) return <Spinner label="Checking your session" />
  if (user === null) return <Navigate to="/login" replace />
  return <Navigate to={HOME_FOR_ROLE[user.role]} replace />
}
