import { useState, type FormEvent, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { appointments as api } from '../../api/endpoints'
import { useToken } from '../../auth/useAuth'
import {
  Alert,
  Button,
  Card,
  CardHeader,
  Field,
  LinkButton,
  PageHeader,
  Spinner,
  TextArea,
  TextInput,
} from '../../components/ui'
import { useAppointment } from '../../hooks/useAppointment'
import { ONE_SECOND, useNow } from '../../hooks/useNow'
import { useAction } from '../../hooks/useResource'
import { doctorName, formatCountdown, formatDateTime } from '../../lib/format'
import './SymptomForm.css'

/** Below this, the countdown turns red — enough warning to finish a sentence and submit. */
const URGENT_MS = 60_000

export function SymptomFormPage(): ReactNode {
  const token = useToken()
  const navigate = useNavigate()
  const { appointmentId = '' } = useParams()

  // `false` so the API drops the hold once its TTL passes: the appointment vanishing from the
  // list is the signal that the slot was released.
  const resource = useAppointment(token, appointmentId, false)
  const appointment = resource.data

  const [symptoms, setSymptoms] = useState('')
  const [durationDays, setDurationDays] = useState('')
  const [notes, setNotes] = useState('')

  const expiresAt = appointment?.hold_expires_at
    ? new Date(appointment.hold_expires_at).getTime()
    : null

  // Ticks every second so the number a patient is watching is the real one. The hold is
  // enforced by the API regardless; this only makes the deadline visible instead of the form
  // failing without warning.
  const now = useNow(ONE_SECOND)
  const remaining = expiresAt === null ? null : expiresAt - now

  const confirm = useAction(async () => {
    const parsed = Number.parseInt(durationDays, 10)
    await api.confirm(token, appointmentId, {
      symptoms: symptoms.trim(),
      duration_days: Number.isFinite(parsed) && parsed >= 0 ? parsed : null,
      additional_notes: notes.trim() || null,
    })
  })

  const release = useAction(async () => {
    await api.cancel(token, appointmentId, 'Released before confirming')
  })

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault()
    const outcome = await confirm.run()
    if (!outcome.ok) return
    navigate('/appointments', { replace: true })
  }

  if (resource.loading) return <Spinner label="Finding your held slot" />

  const expired = remaining !== null && remaining <= 0

  // Gone from the list, or the countdown ran out while this page was open. Both mean the slot
  // is back in circulation and someone else may already have it.
  if (appointment === null || expired) {
    return (
      <>
        <PageHeader title="That slot was released" />
        <Card>
          <Alert tone="warning" title="Your hold expired">
            <p>
              A slot is held only briefly so it cannot sit reserved while nobody is booking it.
              Yours has been released and is available to other patients again.
            </p>
            <p>Nothing was booked, and you have not been charged or notified.</p>
          </Alert>
          <div className="symptom__footer">
            <LinkButton to="/book" variant="primary">
              Choose another time
            </LinkButton>
          </div>
        </Card>
      </>
    )
  }

  if (appointment.status !== 'held') {
    return (
      <>
        <PageHeader title="Already booked" />
        <Card>
          <Alert tone="success" title="This appointment is confirmed">
            You have already completed this booking.
          </Alert>
          <div className="symptom__footer">
            <LinkButton to="/appointments" variant="primary">
              See my appointments
            </LinkButton>
          </div>
        </Card>
      </>
    )
  }

  const urgent = remaining !== null && remaining < URGENT_MS

  return (
    <>
      <PageHeader
        title="Describe your symptoms"
        description="The doctor reads this before your appointment, so start with what is bothering you most."
      />

      <div className={`hold ${urgent ? 'hold--urgent' : ''}`} role="status">
        <div>
          <span className="hold__label">Slot held for you</span>
          <span className="hold__slot">{formatDateTime(appointment.starts_at)}</span>
          <span className="hold__doctor">with {doctorName(appointment.doctor.full_name)}</span>
        </div>
        <div className="hold__timer">
          <span className="hold__time">
            {remaining === null ? '—' : formatCountdown(remaining)}
          </span>
          <span className="hold__caption">until it is released</span>
        </div>
      </div>

      <Card className="symptom">
        <CardHeader
          title="Your symptoms"
          subtitle="Written in your own words. Only you and your doctor can see this."
        />

        {confirm.error && (
          <div className="symptom__error">
            <Alert title="Could not confirm">{confirm.error}</Alert>
          </div>
        )}

        <form onSubmit={onSubmit} noValidate>
          <Field
            label="What is troubling you?"
            htmlFor="symptoms"
            error={confirm.fieldErrors.symptoms}
            hint="Describe it as you would to a person — the main problem first."
          >
            <TextArea
              id="symptoms"
              value={symptoms}
              required
              maxLength={4000}
              placeholder="For example: a sharp pain in my chest when I climb stairs, which has been getting worse."
              invalid={Boolean(confirm.fieldErrors.symptoms)}
              onChange={(event) => setSymptoms(event.target.value)}
            />
          </Field>

          <Field
            label="How many days has this been going on?"
            htmlFor="duration"
            error={confirm.fieldErrors.duration_days}
            hint="Optional. Leave blank if you are not sure."
          >
            <TextInput
              id="duration"
              type="number"
              min={0}
              max={3650}
              value={durationDays}
              invalid={Boolean(confirm.fieldErrors.duration_days)}
              onChange={(event) => setDurationDays(event.target.value)}
            />
          </Field>

          <Field
            label="Anything else the doctor should know?"
            htmlFor="notes"
            hint="Optional. Medicines you take, allergies, or something that seems related."
          >
            <TextArea
              id="notes"
              value={notes}
              maxLength={2000}
              onChange={(event) => setNotes(event.target.value)}
            />
          </Field>

          <div className="symptom__footer">
            <Button
              type="submit"
              variant="primary"
              loading={confirm.pending}
              disabled={symptoms.trim().length === 0}
            >
              Confirm appointment
            </Button>
            <Button
              variant="ghost"
              loading={release.pending}
              onClick={async () => {
                const outcome = await release.run()
                if (outcome.ok) navigate('/book', { replace: true })
              }}
            >
              Release this slot
            </Button>
          </div>
        </form>
      </Card>
    </>
  )
}
