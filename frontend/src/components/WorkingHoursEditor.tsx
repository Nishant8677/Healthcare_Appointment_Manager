/** The weekly schedule editor.
 *
 * The backend rejects a window that does not divide exactly into whole appointments, and
 * explains what would work ("try ending at 16:30 or 17:15"). That message is worth showing
 * verbatim rather than pre-empting with a client-side rule that could disagree with it — so
 * this component collects the hours and lets the API be the authority.
 */

import type { ReactNode } from 'react'

import { WEEKDAY_NAMES } from '../lib/format'
import type { DayDraft } from '../lib/workingHours'
import { Button, TextInput } from './ui'
import './WorkingHoursEditor.css'

export function WorkingHoursEditor({
  week,
  onChange,
}: {
  week: DayDraft[]
  onChange: (week: DayDraft[]) => void
}): ReactNode {
  function update(weekday: number, patch: Partial<DayDraft>): void {
    onChange(week.map((day) => (day.weekday === weekday ? { ...day, ...patch } : day)))
  }

  return (
    <div className="week">
      {week.map((day) => (
        <div className={`week__day ${day.enabled ? 'is-on' : ''}`} key={day.weekday}>
          <label className="week__toggle">
            <input
              type="checkbox"
              checked={day.enabled}
              // Named explicitly rather than relying on the wrapping label: the control means
              // "does this doctor work on Monday", and "on"/"off" is not that sentence.
              aria-label={`Working on ${WEEKDAY_NAMES[day.weekday]}`}
              onChange={(event) => update(day.weekday, { enabled: event.target.checked })}
            />
            <span>{WEEKDAY_NAMES[day.weekday]}</span>
          </label>

          <div className="week__times">
            <label className="sr-only" htmlFor={`start-${day.weekday}`}>
              {WEEKDAY_NAMES[day.weekday]} start time
            </label>
            <TextInput
              id={`start-${day.weekday}`}
              type="time"
              value={day.start_time}
              disabled={!day.enabled}
              onChange={(event) => update(day.weekday, { start_time: event.target.value })}
            />
            <span className="week__dash" aria-hidden="true">
              to
            </span>
            <label className="sr-only" htmlFor={`end-${day.weekday}`}>
              {WEEKDAY_NAMES[day.weekday]} end time
            </label>
            <TextInput
              id={`end-${day.weekday}`}
              type="time"
              value={day.end_time}
              disabled={!day.enabled}
              onChange={(event) => update(day.weekday, { end_time: event.target.value })}
            />
          </div>
        </div>
      ))}

      <div className="week__bulk">
        <Button
          variant="ghost"
          onClick={() =>
            onChange(
              week.map((day) => ({
                ...day,
                start_time: week[0].start_time,
                end_time: week[0].end_time,
              })),
            )
          }
        >
          Copy Monday's times to every day
        </Button>
      </div>
    </div>
  )
}
