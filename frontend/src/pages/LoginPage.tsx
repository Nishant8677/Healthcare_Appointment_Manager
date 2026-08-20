import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { HOME_FOR_ROLE } from '../auth/context'
import { useAuth } from '../auth/useAuth'
import { Alert, Button, Field, TextInput } from '../components/ui'
import { useAction } from '../hooks/useResource'
import { AuthLayout } from './AuthLayout'

export function LoginPage(): ReactNode {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const submit = useAction(signIn)

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault()
    const outcome = await submit.run(email, password)
    if (!outcome.ok) return

    // Back to wherever the guard interrupted them, or their portal's home. The role decides,
    // so one form serves all three portals — there is no "sign in as a doctor" to get wrong.
    const from = (location.state as { from?: string } | null)?.from
    navigate(from ?? HOME_FOR_ROLE[outcome.value.role], { replace: true })
  }

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Patients, doctors and clinic staff all sign in here."
      footer={
        <>
          New patient? <Link to="/register">Create an account</Link>
        </>
      }
    >
      <form onSubmit={onSubmit} noValidate>
        {submit.error && (
          <div className="auth__error">
            <Alert>{submit.error}</Alert>
          </div>
        )}

        <Field label="Email" htmlFor="email" error={submit.fieldErrors.email}>
          <TextInput
            id="email"
            type="email"
            value={email}
            autoComplete="username"
            required
            invalid={Boolean(submit.fieldErrors.email)}
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>

        <Field label="Password" htmlFor="password" error={submit.fieldErrors.password}>
          <TextInput
            id="password"
            type="password"
            value={password}
            autoComplete="current-password"
            required
            invalid={Boolean(submit.fieldErrors.password)}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>

        <Button type="submit" variant="primary" className="auth__submit" loading={submit.pending}>
          Sign in
        </Button>
      </form>
    </AuthLayout>
  )
}
