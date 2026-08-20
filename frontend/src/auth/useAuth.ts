import { useContext } from 'react'

import { AuthContext, type AuthState } from './context'

export function useAuth(): AuthState {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return value
}

/** The token, asserted non-null. For components that only render inside a route guard.
 *
 * The guard has already established there is a session, so threading `token ?? ''` through
 * every call would be noise that also hides a real bug if a guard is ever removed.
 */
export function useToken(): string {
  const { token } = useAuth()
  if (token === null) {
    throw new Error('useToken used outside an authenticated route')
  }
  return token
}
