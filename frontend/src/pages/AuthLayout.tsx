/** The centred card the sign-in and registration pages sit in. */

import type { ReactNode } from 'react'

import './AuthLayout.css'

export function AuthLayout({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string
  subtitle?: string
  children: ReactNode
  footer?: ReactNode
}): ReactNode {
  return (
    <div className="auth">
      <div className="auth__panel">
        <div className="auth__brand">
          <span className="auth__mark" aria-hidden="true" />
          <span>Clinic</span>
        </div>
        <h1 className="auth__title">{title}</h1>
        {subtitle && <p className="auth__subtitle">{subtitle}</p>}
        {children}
      </div>
      {footer && <p className="auth__footer">{footer}</p>}
    </div>
  )
}
