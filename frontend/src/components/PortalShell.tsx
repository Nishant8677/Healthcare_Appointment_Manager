/** The frame around every signed-in screen: identity, navigation, sign out.
 *
 * One shell for all three portals, with the navigation driven by role. Three near-identical
 * layout components would drift, and the portal a person is in should be obvious from the
 * content and the highlighted tab rather than from a different-looking page.
 */

import type { ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import type { UserRole } from '../api/types'
import { useAuth } from '../auth/useAuth'
import { Button } from './ui'
import './PortalShell.css'

interface NavItem {
  to: string
  label: string
  /** `end` for routes that are a prefix of their children, so the tab is not always active. */
  end?: boolean
}

const NAV: Record<UserRole, NavItem[]> = {
  patient: [
    { to: '/appointments', label: 'My appointments', end: true },
    { to: '/book', label: 'Book an appointment' },
    { to: '/settings/calendar', label: 'Calendar' },
  ],
  doctor: [
    { to: '/doctor/schedule', label: 'My schedule' },
    { to: '/settings/calendar', label: 'Calendar' },
  ],
  admin: [
    { to: '/admin/doctors', label: 'Doctors' },
    { to: '/admin/notifications', label: 'Notifications' },
    { to: '/admin/calendar', label: 'Calendar sync' },
  ],
}

const PORTAL_NAME: Record<UserRole, string> = {
  patient: 'Patient portal',
  doctor: 'Doctor portal',
  admin: 'Clinic administration',
}

export function PortalShell(): ReactNode {
  const { user, signOut } = useAuth()
  if (user === null) return null

  return (
    <div className="shell">
      <header className="shell__bar">
        <div className="shell__inner">
          <div className="shell__brand">
            <span className="shell__mark" aria-hidden="true" />
            <div>
              <span className="shell__name">Clinic</span>
              <span className="shell__portal">{PORTAL_NAME[user.role]}</span>
            </div>
          </div>

          <nav className="shell__nav" aria-label="Sections">
            {NAV[user.role].map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `shell__link ${isActive ? 'is-active' : ''}`}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="shell__account">
            <span className="shell__user" title={user.email}>
              {user.full_name}
            </span>
            <Button variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className="shell__main">
        <Outlet />
      </main>
    </div>
  )
}
