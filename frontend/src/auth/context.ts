import { createContext } from 'react'

import type { User, UserRole } from '../api/types'

export interface AuthState {
  /** `null` once we know nobody is signed in; `undefined` while we are still finding out. */
  user: User | null
  token: string | null
  /** True until the stored token has been checked against the API on first load. */
  loading: boolean
  signIn: (email: string, password: string) => Promise<User>
  signOut: () => void
}

export const AuthContext = createContext<AuthState | null>(null)

/** Where each role lands after signing in. Also the redirect for a role-forbidden route. */
export const HOME_FOR_ROLE: Record<UserRole, string> = {
  patient: '/appointments',
  doctor: '/doctor/schedule',
  admin: '/admin/doctors',
}
