import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { appointments as api } from '../../api/endpoints'
import type { Appointment } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { AppointmentCard } from '../../components/AppointmentCard'
import { Alert, Button, DataState, EmptyState, LinkButton, PageHeader } from '../../components/ui'
import { ONE_MINUTE, useNow } from '../../hooks/useNow'
import { useAction, useResource } from '../../hooks/useResource'
import { isCancelled } from '../../lib/format'

export function AppointmentsPage(): ReactNode {
  const token = useToken()
  const [showCancelled, setShowCancelled] = useState(false)

  const resource = useResource(
    () => api.list(token, showCancelled),
    [token, showCancelled],
  )

  const cancel = useAction(async (id: string) => {
    await api.cancel(token, id, 'Cancelled by the patient')
    resource.reload()
  })

  // A minute is fine here: the boundary between "upcoming" and "past" only ever moves by
  // whole appointments, and a per-second tick would re-render the whole list for nothing.
  const now = useNow(ONE_MINUTE)

  const { upcoming, past } = useMemo(() => {
    const all = resource.data ?? []
    return {
      upcoming: all.filter(
        (item) => !isCancelled(item.status) && new Date(item.starts_at).getTime() >= now,
      ),
      past: all.filter(
        (item) => isCancelled(item.status) || new Date(item.starts_at).getTime() < now,
      ),
    }
  }, [resource.data, now])

  function cancelActions(appointment: Appointment): ReactNode {
    // A held slot is still being booked; the symptom form is where it is released or
    // confirmed, so offering "cancel" here would be a second, confusing route out of it.
    if (appointment.status !== 'confirmed') return null
    return (
      <Button variant="danger" loading={cancel.pending} onClick={() => cancel.run(appointment.id)}>
        Cancel
      </Button>
    )
  }

  return (
    <>
      <PageHeader
        title="My appointments"
        description="Everything you have booked, and everything that has already happened."
        actions={<LinkButton to="/book" variant="primary">Book an appointment</LinkButton>}
      />

      {cancel.error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert title="Could not cancel">{cancel.error}</Alert>
        </div>
      )}

      <DataState
        resource={resource}
        empty={
          <EmptyState
            title="No appointments yet"
            action={<LinkButton to="/book" variant="primary">Find a doctor</LinkButton>}
          >
            When you book one, it will appear here with a link to your visit summary
            afterwards.
          </EmptyState>
        }
      >
        {() => (
          <>
            <section className="section">
              <div className="section__heading">
                <h2>Upcoming</h2>
                <span className="section__count">{upcoming.length}</span>
              </div>
              {upcoming.length === 0 ? (
                <EmptyState title="Nothing coming up">
                  <Link to="/book">Book an appointment</Link> to see it here.
                </EmptyState>
              ) : (
                <div className="appt-list">
                  {upcoming.map((appointment) => (
                    <AppointmentCard
                      key={appointment.id}
                      appointment={appointment}
                      viewer="patient"
                      to={`/appointments/${appointment.id}`}
                      actions={cancelActions(appointment)}
                    />
                  ))}
                </div>
              )}
            </section>

            <section className="section">
              <div className="section__heading">
                <h2>Past</h2>
                <span className="section__count">{past.length}</span>
                <Button variant="ghost" onClick={() => setShowCancelled((value) => !value)}>
                  {showCancelled ? 'Hide cancelled' : 'Show cancelled'}
                </Button>
              </div>
              {past.length === 0 ? (
                <EmptyState title="Nothing here yet" />
              ) : (
                <div className="appt-list">
                  {past.map((appointment) => (
                    <AppointmentCard
                      key={appointment.id}
                      appointment={appointment}
                      viewer="patient"
                      to={`/appointments/${appointment.id}`}
                    />
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </DataState>
    </>
  )
}
