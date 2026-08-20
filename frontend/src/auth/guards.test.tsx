/** Route guards: who is allowed to see which portal.
 *
 * The API enforces roles on every endpoint, so this is not the security boundary. It is the
 * reason a patient never sees a doctor's screen fail piecemeal with 403s — and, more to the
 * point, the reason a doctor's AI triage brief is never fetched into a patient's browser.
 */

import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { User, UserRole } from '../api/types'
import { AuthContext, type AuthState } from './context'
import { RequireRole } from './guards'

function userWithRole(role: UserRole): User {
  return {
    id: 'user-1',
    email: `${role}@example.com`,
    full_name: 'Test Person',
    role,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
  }
}

function renderAt(path: string, state: Partial<AuthState>): void {
  const value: AuthState = {
    user: null,
    token: null,
    loading: false,
    signIn: async () => userWithRole('patient'),
    signOut: () => {},
    ...state,
  }

  function Wrapper({ children }: { children: ReactNode }): ReactNode {
    return <AuthContext value={value}>{children}</AuthContext>
  }

  render(
    <Wrapper>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<p>Sign in page</p>} />
          <Route path="/appointments" element={<p>Patient home</p>} />
          <Route path="/doctor/schedule" element={<p>Doctor home</p>} />
          <Route element={<RequireRole allow={['doctor']} />}>
            <Route path="/doctor/secret" element={<p>Doctor only</p>} />
          </Route>
          <Route element={<RequireRole allow={['admin']} />}>
            <Route path="/admin/doctors" element={<p>Admin only</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </Wrapper>,
  )
}

describe('RequireRole', () => {
  it('lets the right role through', () => {
    renderAt('/doctor/secret', { user: userWithRole('doctor'), token: 't' })
    expect(screen.getByText('Doctor only')).toBeInTheDocument()
  })

  it('sends a patient away from a doctor route, to their own portal', () => {
    renderAt('/doctor/secret', { user: userWithRole('patient'), token: 't' })
    expect(screen.queryByText('Doctor only')).not.toBeInTheDocument()
    expect(screen.getByText('Patient home')).toBeInTheDocument()
  })

  it('sends a doctor away from an admin route', () => {
    renderAt('/admin/doctors', { user: userWithRole('doctor'), token: 't' })
    expect(screen.queryByText('Admin only')).not.toBeInTheDocument()
    expect(screen.getByText('Doctor home')).toBeInTheDocument()
  })

  it('sends a signed-out visitor to sign in', () => {
    renderAt('/admin/doctors', { user: null, token: null })
    expect(screen.getByText('Sign in page')).toBeInTheDocument()
  })

  it('waits rather than bouncing while the stored token is being checked', () => {
    // Without this, refreshing a deep link would throw the user out to the sign-in page for
    // the moment it takes to validate a perfectly good session.
    renderAt('/doctor/secret', { user: null, token: 'stored', loading: true })
    expect(screen.queryByText('Sign in page')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
