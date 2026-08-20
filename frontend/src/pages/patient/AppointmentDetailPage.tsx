import { useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { appointments as api, doctors as doctorsApi } from '../../api/endpoints'
import { ApiError } from '../../api/client'
import { useToken } from '../../auth/useAuth'
import { PostVisitSummaryPanel } from '../../components/SummaryPanel'
import {
  Alert,
  Button,
  Card,
  CardHeader,
  EmptyState,
  LinkButton,
  PageHeader,
  Pill,
  Spinner,
} from '../../components/ui'
import { useAppointment } from '../../hooks/useAppointment'
import { useAction, useResource } from '../../hooks/useResource'
import {
  addDays,
  appointmentStatus,
  doctorName,
  formatDateTime,
  formatTime,
  isCancelled,
  toDateInputValue,
} from '../../lib/format'

export function AppointmentDetailPage(): ReactNode {
  const token = useToken()
  const navigate = useNavigate()
  const { appointmentId = '' } = useParams()
  const [rescheduling, setRescheduling] = useState(false)
  const [date, setDate] = useState(() => toDateInputValue(addDays(new Date(), 1)))

  const resource = useAppointment(token, appointmentId)
  const appointment = resource.data

  const summary = useResource(async () => {
    try {
      return await api.postVisitSummary(token, appointmentId)
    } catch (error) {
      // Before the visit there is nothing to summarise; the API says so with a 404 and this
      // screen should stay quiet rather than showing a failure.
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  }, [token, appointmentId])

  const availability = useResource(
    async () =>
      appointment && rescheduling
        ? doctorsApi.slots(token, appointment.doctor.id, date)
        : null,
    [token, appointment?.doctor.id, date, rescheduling],
  )

  const cancel = useAction(async () => {
    await api.cancel(token, appointmentId, 'Cancelled by the patient')
    resource.reload()
  })

  const reschedule = useAction(async (startsAt: string) => {
    const moved = await api.reschedule(token, appointmentId, startsAt)
    navigate(`/appointments/${moved.id}`, { replace: true })
    setRescheduling(false)
  })

  if (resource.loading) return <Spinner label="Loading your appointment" />
  if (resource.error !== null) return <Alert title="Could not load this">{resource.error}</Alert>

  if (appointment === null) {
    return (
      <EmptyState title="Appointment not found">
        It may have been removed, or the link is wrong.
      </EmptyState>
    )
  }

  const status = appointmentStatus(appointment.status, true)
  const canChange = appointment.status === 'confirmed'

  return (
    <>
      <PageHeader
        title={doctorName(appointment.doctor.full_name)}
        description={appointment.doctor.specialisation}
        actions={<LinkButton to="/appointments">All appointments</LinkButton>}
      />

      <div className="stack">
        <Card>
          <CardHeader
            title={formatDateTime(appointment.starts_at)}
            subtitle={`Ends at ${formatTime(appointment.ends_at)}`}
            actions={<Pill tone={status.tone}>{status.label}</Pill>}
          />

          {appointment.cancellation_reason && (
            <Alert tone="neutral">{appointment.cancellation_reason}</Alert>
          )}

          {canChange && (
            <div className="symptom__footer">
              <Button variant="secondary" onClick={() => setRescheduling((value) => !value)}>
                {rescheduling ? 'Keep this time' : 'Reschedule'}
              </Button>
              <Button variant="danger" loading={cancel.pending} onClick={() => cancel.run()}>
                Cancel appointment
              </Button>
            </div>
          )}

          {cancel.error && (
            <div style={{ marginTop: '1rem' }}>
              <Alert title="Could not cancel">{cancel.error}</Alert>
            </div>
          )}
        </Card>

        {rescheduling && canChange && (
          <Card>
            <CardHeader
              title="Move this appointment"
              subtitle="Pick another time with the same doctor. Your symptom form comes with it."
            />

            {reschedule.error && (
              <div style={{ marginBottom: '1rem' }}>
                <Alert title="Could not move it">{reschedule.error}</Alert>
              </div>
            )}

            <div className="days">
              <div className="days__picker" style={{ marginLeft: 0 }}>
                <label className="sr-only" htmlFor="reschedule-date">
                  New date
                </label>
                <input
                  id="reschedule-date"
                  type="date"
                  className="input"
                  value={date}
                  min={toDateInputValue(new Date())}
                  onChange={(event) => setDate(event.target.value)}
                />
              </div>
            </div>

            {availability.loading ? (
              <Spinner label="Looking for free times" />
            ) : availability.data && availability.data.slots.length > 0 ? (
              <ul className="slots">
                {availability.data.slots.map((slot) => (
                  <li key={slot.starts_at}>
                    <Button
                      variant="secondary"
                      className="slot"
                      loading={reschedule.pending}
                      onClick={() => reschedule.run(slot.starts_at)}
                    >
                      {formatTime(slot.starts_at)}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState title="No free times on this day" />
            )}
          </Card>
        )}

        {appointment.symptom_report && (
          <Card>
            <CardHeader title="What you told us" subtitle="Sent to the doctor before your visit." />
            <p className="summary__prose">{appointment.symptom_report.symptoms}</p>
            {appointment.symptom_report.duration_days !== null && (
              <p className="summary__meta">
                Going on for {appointment.symptom_report.duration_days} day
                {appointment.symptom_report.duration_days === 1 ? '' : 's'}.
              </p>
            )}
            {appointment.symptom_report.additional_notes && (
              <>
                <h3 className="summary__heading">Additional notes</h3>
                <p className="summary__prose">{appointment.symptom_report.additional_notes}</p>
              </>
            )}
          </Card>
        )}

        {/* Only after the visit. Before it there is nothing to summarise, and a panel saying
            so on every upcoming appointment would be noise. */}
        {(appointment.status === 'completed' || summary.data !== null) && (
          <PostVisitSummaryPanel
            summary={summary.data}
            loading={summary.loading}
            error={summary.error}
          />
        )}

        {isCancelled(appointment.status) && (
          <EmptyState
            title="This appointment did not happen"
            action={<LinkButton to="/book" variant="primary">Book another</LinkButton>}
          />
        )}
      </div>
    </>
  )
}
