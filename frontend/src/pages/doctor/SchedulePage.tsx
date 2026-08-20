import { useMemo, type ReactNode } from 'react'

import { appointments as api } from '../../api/endpoints'
import type { Appointment } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { AppointmentCard } from '../../components/AppointmentCard'
import { DataState, EmptyState, PageHeader, Pill } from '../../components/ui'
import { useResource } from '../../hooks/useResource'
import { formatDayAndDate, toDateInputValue } from '../../lib/format'

/** Grouped by day rather than listed flat: a doctor's question is "what does today look
 * like", and a single scrolling list makes them count rows to answer it. */
export function SchedulePage(): ReactNode {
  const token = useToken()
  const resource = useResource(() => api.list(token), [token])

  const days = useMemo(() => groupByDay(resource.data ?? []), [resource.data])
  const todayKey = toDateInputValue(new Date())

  return (
    <>
      <PageHeader
        title="My schedule"
        description="Your upcoming consultations. Open one to read the pre-visit brief and record your notes."
      />

      <DataState
        resource={resource}
        empty={
          <EmptyState title="Nothing booked">
            Appointments patients book with you will appear here.
          </EmptyState>
        }
      >
        {() =>
          days.length === 0 ? (
            <EmptyState title="Nothing booked" />
          ) : (
            <>
              {days.map(([day, items]) => (
                <section className="section" key={day}>
                  <div className="section__heading">
                    <h2>{formatDayAndDate(items[0].starts_at)}</h2>
                    {day === todayKey && <Pill tone="accent">Today</Pill>}
                    <span className="section__count">
                      {items.length} appointment{items.length === 1 ? '' : 's'}
                    </span>
                  </div>
                  <div className="appt-list">
                    {items.map((appointment) => (
                      <AppointmentCard
                        key={appointment.id}
                        appointment={appointment}
                        viewer="doctor"
                        to={`/doctor/appointments/${appointment.id}`}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </>
          )
        }
      </DataState>
    </>
  )
}

/** Keyed on the *local* calendar day. `toDateInputValue` is used rather than slicing the ISO
 * string, which would group by the UTC day and split an evening clinic across two headings
 * for anyone east of Greenwich. */
function groupByDay(items: Appointment[]): Array<[string, Appointment[]]> {
  const byDay = new Map<string, Appointment[]>()
  for (const appointment of items) {
    const key = toDateInputValue(new Date(appointment.starts_at))
    const bucket = byDay.get(key)
    if (bucket) bucket.push(appointment)
    else byDay.set(key, [appointment])
  }
  return [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b))
}
