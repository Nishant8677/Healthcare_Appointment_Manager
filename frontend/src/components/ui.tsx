/** The shared vocabulary: buttons, cards, fields, pills, and the three states every screen
 * that loads something has to handle.
 *
 * Kept in one module because they are small and always used together. The one that earns its
 * place most is `DataState` — without it every screen re-invents "spinner, then error, then
 * empty, then content", and the empty and error cases are the ones that get skipped.
 */

import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import { Link } from 'react-router-dom'

import type { Tone } from '../lib/format'
import './ui.css'

/* ----------------------------------------------------------------- buttons */

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  /** Shows a spinner and blocks further clicks. Prevents double-submitting a booking. */
  loading?: boolean
}

export function Button({
  variant = 'secondary',
  loading = false,
  disabled,
  children,
  className = '',
  ...rest
}: ButtonProps): ReactNode {
  return (
    <button
      type="button"
      className={`btn btn--${variant} ${className}`.trim()}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && <span className="btn__spinner" aria-hidden="true" />}
      {children}
    </button>
  )
}

/** A navigation link that looks like a button.
 *
 * Not a `<Button>` wrapping a `<Link>`: an anchor inside a button is invalid HTML, and
 * browsers and screen readers disagree about what it even is. This stays an anchor — so
 * middle-click, "open in new tab" and the status bar all work — and only borrows the styling.
 */
export function LinkButton({
  to,
  variant = 'secondary',
  children,
}: {
  to: string
  variant?: ButtonVariant
  children: ReactNode
}): ReactNode {
  return (
    <Link to={to} className={`btn btn--${variant} btn--link`}>
      {children}
    </Link>
  )
}

/* ----------------------------------------------------------------- surfaces */

export function Card({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}): ReactNode {
  return <section className={`card ${className}`.trim()}>{children}</section>
}

export function CardHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
}): ReactNode {
  return (
    <header className="card__header">
      <div>
        <h2 className="card__title">{title}</h2>
        {subtitle && <p className="card__subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="card__actions">{actions}</div>}
    </header>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
}): ReactNode {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="page-header__description">{description}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  )
}

/* ----------------------------------------------------------------- status */

export function Pill({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }): ReactNode {
  return <span className={`pill pill--${tone}`}>{children}</span>
}

export function Alert({
  tone = 'danger',
  title,
  children,
}: {
  tone?: Tone
  title?: ReactNode
  children?: ReactNode
}): ReactNode {
  return (
    // `role="alert"` so a screen reader announces a failed booking rather than leaving it to
    // be discovered by chance.
    <div className={`alert alert--${tone}`} role="alert">
      {title && <strong className="alert__title">{title}</strong>}
      {children && <div className="alert__body">{children}</div>}
    </div>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }): ReactNode {
  return (
    <div className="spinner" role="status">
      <span className="spinner__ring" aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </div>
  )
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string
  children?: ReactNode
  action?: ReactNode
}): ReactNode {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      {children && <p className="empty__body">{children}</p>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  )
}

/** Renders the right thing for a resource: spinner, error, empty, or the content.
 *
 * `data` is passed to the child as a non-null value, so screens do not thread `data?.` through
 * their whole tree — and cannot forget that it might not have arrived.
 */
export function DataState<T>({
  resource,
  empty,
  children,
}: {
  resource: { data: T | null; error: string | null; loading: boolean }
  empty?: ReactNode
  children: (data: T) => ReactNode
}): ReactNode {
  if (resource.loading && resource.data === null) return <Spinner />
  if (resource.error !== null) return <Alert title="Could not load this">{resource.error}</Alert>
  if (resource.data === null) return empty ?? null
  if (Array.isArray(resource.data) && resource.data.length === 0 && empty) return empty
  return children(resource.data)
}

/* ----------------------------------------------------------------- forms */

interface FieldProps {
  label: string
  htmlFor: string
  error?: string
  hint?: ReactNode
  children: ReactNode
}

export function Field({ label, htmlFor, error, hint, children }: FieldProps): ReactNode {
  return (
    <div className="field">
      <label className="field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && !error && <p className="field__hint">{hint}</p>}
      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

export function TextInput({
  invalid,
  className = '',
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }): ReactNode {
  return (
    <input
      className={`input ${invalid ? 'input--invalid' : ''} ${className}`.trim()}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  )
}

export function TextArea({
  invalid,
  className = '',
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }): ReactNode {
  return (
    <textarea
      className={`input input--area ${invalid ? 'input--invalid' : ''} ${className}`.trim()}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  )
}

export function Select({
  className = '',
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>): ReactNode {
  return (
    <select className={`input input--select ${className}`.trim()} {...rest}>
      {children}
    </select>
  )
}
