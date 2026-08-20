import { useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { appointments as appointmentsApi, doctors as doctorsApi } from '../../api/endpoints'
import { useToken } from '../../auth/useAuth'
import {
  Alert,
  Button,
  Card,
  DataState,
  EmptyState,
  LinkButton,
  PageHeader,
} from '../../components/ui'
import { useAction, useResource } from '../../hooks/useResource'
import { addDays, doctorName, formatTime, toDateInputValue } from '../../lib/format'
import './BookSlot.css'

/** How many days the quick-pick strip offers. The real limit is the API's booking horizon,
 * which is configuration the client does not know — so the strip is a convenience and the
 * date input can go further, with the server's message shown if it refuses. */
const QUICK_DAYS = 7

export function BookSlotPage(): ReactNode {
  const token = useToken()
  const navigate = useNavigate()
  const { doctorId = '' } = useParams()

  const today = new Date()
  const [date, setDate] = useState(() => toDateInputValue(today))

  const doctorList = useResource(() => doctorsApi.list(token), [token])
  const doctor = (doctorList.data ?? []).find((item) => item.id === doctorId)

  const availability = useResource(
    () => doctorsApi.slots(token, doctorId, date),
    [token, doctorId, date],
  )

  const hold = useAction(async (startsAt: string) => {
    const held = await appointmentsApi.hold(token, { doctor_id: doctorId, starts_at: startsAt })
    navigate(`/book/hold/${held.id}`)
  })

  const quickDays = Array.from({ length: QUICK_DAYS }, (_, index) => addDays(today, index))

  return (
    <>
      <PageHeader
        title={doctor ? doctorName(doctor.full_name) : 'Choose a time'}
        description={
          doctor
            ? `${doctor.specialisation} · ${doctor.slot_duration_minutes}-minute appointments`
            : undefined
        }
        actions={<LinkButton to="/book">Back to doctors</LinkButton>}
      />

      {hold.error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert title="That did not work">
            {hold.error}
            {/* A lost race is the expected reason: somebody else confirmed the same slot
                between this page loading and the button being pressed. */}
            <p>
              <Button variant="ghost" onClick={() => availability.reload()}>
                Refresh the times
              </Button>
            </p>
          </Alert>
        </div>
      )}

      <Card>
        <div className="days">
          {quickDays.map((day) => {
            const value = toDateInputValue(day)
            return (
              <button
                key={value}
                type="button"
                className={`day ${value === date ? 'is-active' : ''}`}
                onClick={() => setDate(value)}
                aria-pressed={value === date}
                // The visible label is an abbreviated "FRI 21", which a screen reader reads
                // as two disconnected fragments. The full date is unambiguous.
                aria-label={day.toLocaleDateString(undefined, {
                  weekday: 'long',
                  day: 'numeric',
                  month: 'long',
                })}
              >
                <span className="day__name">
                  {day.toLocaleDateString(undefined, { weekday: 'short' })}
                </span>
                <span className="day__number">{day.getDate()}</span>
              </button>
            )
          })}

          <div className="days__picker">
            <label className="sr-only" htmlFor="date">
              Or choose a date
            </label>
            <input
              id="date"
              type="date"
              className="input"
              value={date}
              min={toDateInputValue(today)}
              onChange={(event) => setDate(event.target.value)}
            />
          </div>
        </div>

        <DataState resource={availability}>
          {(data) =>
            data.slots.length === 0 ? (
              <EmptyState title="No free times on this day">
                Try another date — the doctor may not be working, or every slot is taken.
              </EmptyState>
            ) : (
              <>
                <p className="slots__note">
                  {data.slots.length} time{data.slots.length === 1 ? '' : 's'} available.
                  Choosing one holds it for you while you describe your symptoms.
                </p>
                <ul className="slots">
                  {data.slots.map((slot) => (
                    <li key={slot.starts_at}>
                      <Button
                        variant="secondary"
                        className="slot"
                        loading={hold.pending}
                        onClick={() => hold.run(slot.starts_at)}
                      >
                        {formatTime(slot.starts_at)}
                      </Button>
                    </li>
                  ))}
                </ul>
              </>
            )
          }
        </DataState>
      </Card>
    </>
  )
}
