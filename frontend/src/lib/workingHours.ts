/** Converting between the API's working-hours rows and the week the editor renders.
 *
 * Separate from the editor component so the module exports components only — and because
 * these are the parts worth testing directly: the `HH:MM:SS` to `HH:MM` trim is the sort of
 * thing that silently produces an invalid `<input type="time">` value.
 */

import type { WorkingHoursItem } from '../api/types'
import { WEEKDAY_NAMES } from './format'

export interface DayDraft {
  weekday: number
  enabled: boolean
  start_time: string
  end_time: string
}

const DEFAULT_START = '09:00'
const DEFAULT_END = '17:00'

export function emptyWeek(): DayDraft[] {
  return WEEKDAY_NAMES.map((_, weekday) => ({
    weekday,
    // Monday to Friday on by default: the common case, and an admin who wants weekends can
    // tick two boxes rather than five.
    enabled: weekday < 5,
    start_time: DEFAULT_START,
    end_time: DEFAULT_END,
  }))
}

export function weekFromWorkingHours(hours: WorkingHoursItem[]): DayDraft[] {
  return WEEKDAY_NAMES.map((_, weekday) => {
    const existing = hours.find((item) => item.weekday === weekday)
    return {
      weekday,
      enabled: existing !== undefined,
      // The API returns `HH:MM:SS`; `<input type="time">` rejects the seconds and silently
      // renders empty, which looks like a doctor with no hours set.
      start_time: existing ? existing.start_time.slice(0, 5) : DEFAULT_START,
      end_time: existing ? existing.end_time.slice(0, 5) : DEFAULT_END,
    }
  })
}

export function toWorkingHours(week: DayDraft[]): WorkingHoursItem[] {
  return week
    .filter((day) => day.enabled)
    .map((day) => ({
      weekday: day.weekday,
      start_time: day.start_time,
      end_time: day.end_time,
    }))
}
