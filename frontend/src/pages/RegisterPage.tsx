import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { auth as authApi } from '../api/endpoints'
import { useAuth } from '../auth/useAuth'
import { Alert, Button, Field, TextInput } from '../components/ui'
import { useAction } from '../hooks/useResource'
import { AuthLayout } from './AuthLayout'

/** Matches the backend's minimum. Checked here only to fail before a round trip — the API is
 * what enforces it, and its message is what gets shown if the two ever disagree. */
const MIN_PASSWORD_LENGTH = 10

export function RegisterPage(): ReactNode {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const submit = useAction(async () => {
    await authApi.register({ email, password, full_name: fullName })
    // Straight in rather than bouncing to the sign-in form: they have just typed these
    // credentials, and asking for them again is friction with nothing behind it.
    await signIn(email, password)
  })

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault()
    const outcome = await submit.run()
    // Without this guard a failed registration would still navigate, landing the visitor on
    // a signed-out patient portal that immediately bounces them back to sign in.
    if (!outcome.ok) return
    navigate('/appointments', { replace: true })
  }

  const passwordTooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH

  return (
    <AuthLayout
      title="Create your account"
      subtitle="This registers you as a patient. Doctor and staff accounts are created by the clinic."
      footer={
        <>
          Already registered? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <form onSubmit={onSubmit} noValidate>
        {submit.error && (
          <div className="auth__error">
            <Alert>{submit.error}</Alert>
          </div>
        )}

        <Field label="Full name" htmlFor="full_name" error={submit.fieldErrors.full_name}>
          <TextInput
            id="full_name"
            value={fullName}
            autoComplete="name"
            required
            invalid={Boolean(submit.fieldErrors.full_name)}
            onChange={(event) => setFullName(event.target.value)}
          />
        </Field>

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

        <Field
          label="Password"
          htmlFor="password"
          hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
          error={
            submit.fieldErrors.password ??
            (passwordTooShort ? `Use at least ${MIN_PASSWORD_LENGTH} characters.` : undefined)
          }
        >
          <TextInput
            id="password"
            type="password"
            value={password}
            autoComplete="new-password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            invalid={Boolean(submit.fieldErrors.password) || passwordTooShort}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>

        <Button
          type="submit"
          variant="primary"
          className="auth__submit"
          loading={submit.pending}
          disabled={passwordTooShort}
        >
          Create account
        </Button>
      </form>
    </AuthLayout>
  )
}
