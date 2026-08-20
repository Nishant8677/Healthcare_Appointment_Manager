/** Turning API values into things a person can read.
 *
 * Pure functions, no React, so they can be tested directly — which matters more than it looks
 * for the appointment statuses: the backend has two distinct cancellation states and the
 * difference (who cancelled) is the whole point of them being separate.
 */

import type { AppointmentStatus, CalendarSyncStatus, NotificationStatus, UrgencyLevel } from '../api/types'

/** 0 = Monday, matching Python's `date.weekday()` and the backend's working-hours column. */
export const WEEKDAY_NAMES = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
] as const

export function weekdayName(weekday: number): string {
  return WEEKDAY_NAMES[weekday] ?? `Day ${weekday}`
}

/** Times are rendered in the browser's zone. The API always sends an instant with an offset,
 * so this is a display choice rather than a conversion that could go wrong. */
export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

export function formatDayAndDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })
}

export function formatDateTime(iso: string): string {
  return `${formatDate(iso)} at ${formatTime(iso)}`
}

/** `yyyy-mm-dd` in *local* time, for `<input type="date">` and the API's date query params.
 *
 * Not `toISOString().slice(0, 10)`, which converts to UTC first and so returns yesterday for
 * anyone east of Greenwich for part of every day. That bug is invisible in London and
 * constant in Kolkata. */
export function toDateInputValue(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

export function addDays(date: Date, days: number): Date {
  const copy = new Date(date)
  copy.setDate(copy.getDate() + days)
  return copy
}

/** `m:ss` for the slot-hold countdown. Clamped at zero rather than going negative. */
export function formatCountdown(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info'

interface Descriptor {
  label: string
  tone: Tone
}

const APPOINTMENT_STATUS: Record<AppointmentStatus, Descriptor> = {
  held: { label: 'Holding slot', tone: 'warning' },
  confirmed: { label: 'Confirmed', tone: 'success' },
  completed: { label: 'Completed', tone: 'info' },
  cancelled_by_patient: { label: 'Cancelled by you', tone: 'neutral' },
  cancelled_by_clinic: { label: 'Cancelled by the clinic', tone: 'danger' },
}

export function appointmentStatus(status: AppointmentStatus, viewerIsPatient = true): Descriptor {
  const descriptor = APPOINTMENT_STATUS[status]
  if (status === 'cancelled_by_patient' && !viewerIsPatient) {
    // "Cancelled by you" is only true for the patient reading their own booking.
    return { label: 'Cancelled by the patient', tone: 'neutral' }
  }
  return descriptor ?? { label: status, tone: 'neutral' }
}

export function isCancelled(status: AppointmentStatus): boolean {
  return status === 'cancelled_by_patient' || status === 'cancelled_by_clinic'
}

const URGENCY: Record<UrgencyLevel, Descriptor> = {
  low: { label: 'Low urgency', tone: 'success' },
  medium: { label: 'Medium urgency', tone: 'warning' },
  high: { label: 'High urgency', tone: 'danger' },
}

export function urgency(level: UrgencyLevel): Descriptor {
  return URGENCY[level] ?? { label: level, tone: 'neutral' }
}

const NOTIFICATION: Record<NotificationStatus, Descriptor> = {
  pending: { label: 'Queued', tone: 'info' },
  sent: { label: 'Sent', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
}

export function notificationStatus(status: NotificationStatus): Descriptor {
  return NOTIFICATION[status] ?? { label: status, tone: 'neutral' }
}

const CALENDAR: Record<CalendarSyncStatus, Descriptor> = {
  pending: { label: 'Queued', tone: 'info' },
  synced: { label: 'In sync', tone: 'success' },
  // Not a failure: the user simply has no calendar connected, which is the normal case.
  skipped: { label: 'No calendar', tone: 'neutral' },
  failed: { label: 'Failed', tone: 'danger' },
}

export function calendarStatus(status: CalendarSyncStatus): Descriptor {
  return CALENDAR[status] ?? { label: status, tone: 'neutral' }
}

export function humanise(value: string): string {
  const spaced = value.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}


/** Titles that mean "do not add another one".
 *
 * The optional full stop plus the required space do the word-boundary work here:
 * "Dr " and "Dr. " match, "Drew Patel" does not.
 */
const HONORIFICS = /^(dr|doctor|prof|professor|mr|mrs|ms|miss|mx)[.]? /i

/** A doctor's name with a "Dr" in front of it — but only one.
 *
 * Names arrive from whatever an admin typed. Some clinics enter "Asha Rao", others "Dr Asha
 * Rao", and blindly prefixing produces "Dr Dr Asha Rao" on every screen for half the records.
 * Cheap to get right, and conspicuous to get wrong.
 */
export function doctorName(fullName: string): string {
  const trimmed = fullName.trim()
  return HONORIFICS.test(trimmed) ? trimmed : `Dr ${trimmed}`
}
