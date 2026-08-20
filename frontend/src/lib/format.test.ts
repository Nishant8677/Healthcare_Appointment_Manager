import { describe, expect, it } from 'vitest'

import {
  appointmentStatus,
  doctorName,
  calendarStatus,
  formatCountdown,
  isCancelled,
  toDateInputValue,
  weekdayName,
} from './format'

describe('toDateInputValue', () => {
  it('uses the local calendar day, not the UTC one', () => {
    // 23:30 on the 20th in a zone ahead of UTC is still the 20th locally but the 20th
    // *or the 21st* in UTC depending on the offset. `toISOString().slice(0, 10)` — the
    // obvious implementation — returns the UTC day and so silently books the wrong date for
    // anyone east of Greenwich late in the evening.
    const lateEvening = new Date(2026, 7, 20, 23, 30)
    expect(toDateInputValue(lateEvening)).toBe('2026-08-20')
  })

  it('pads single-digit months and days', () => {
    expect(toDateInputValue(new Date(2026, 0, 5))).toBe('2026-01-05')
  })
})

describe('formatCountdown', () => {
  it('renders minutes and zero-padded seconds', () => {
    expect(formatCountdown(4 * 60_000 + 7_000)).toBe('4:07')
  })

  it('clamps at zero rather than counting into negatives', () => {
    // The hold can lapse while the tab is open; a "-0:03" would be alarming and meaningless.
    expect(formatCountdown(-5_000)).toBe('0:00')
  })

  it('floors partial seconds so the number never reads higher than the time left', () => {
    expect(formatCountdown(59_999)).toBe('0:59')
  })
})

describe('appointmentStatus', () => {
  it('tells a patient they cancelled it themselves', () => {
    expect(appointmentStatus('cancelled_by_patient', true).label).toBe('Cancelled by you')
  })

  it('tells a doctor the patient cancelled', () => {
    // The same status, read by two people, means two different sentences. Getting this wrong
    // would show a doctor "Cancelled by you" for a patient's cancellation.
    expect(appointmentStatus('cancelled_by_patient', false).label).toBe('Cancelled by the patient')
  })

  it('distinguishes a clinic cancellation, which is the one that needs attention', () => {
    const clinic = appointmentStatus('cancelled_by_clinic', true)
    expect(clinic.label).toBe('Cancelled by the clinic')
    expect(clinic.tone).toBe('danger')
  })

  it('treats both cancellation states as cancelled', () => {
    expect(isCancelled('cancelled_by_patient')).toBe(true)
    expect(isCancelled('cancelled_by_clinic')).toBe(true)
    expect(isCancelled('confirmed')).toBe(false)
    expect(isCancelled('held')).toBe(false)
  })
})

describe('calendarStatus', () => {
  it('does not present "no calendar connected" as a failure', () => {
    // Most patients never connect one. Colouring this red would bury the rows that matter.
    const skipped = calendarStatus('skipped')
    expect(skipped.tone).toBe('neutral')
    expect(skipped.label).toBe('No calendar')
  })

  it('marks a genuine failure as one', () => {
    expect(calendarStatus('failed').tone).toBe('danger')
  })
})

describe('weekdayName', () => {
  it('treats 0 as Monday, matching the API', () => {
    // The backend stores Python's `date.weekday()`, where Monday is 0. JavaScript's
    // `Date.getDay()` puts Sunday at 0 — mixing them shifts every schedule by a day.
    expect(weekdayName(0)).toBe('Monday')
    expect(weekdayName(6)).toBe('Sunday')
  })
})

describe('doctorName', () => {
  it('adds the title when the stored name has none', () => {
    expect(doctorName('Asha Rao')).toBe('Dr Asha Rao')
  })

  it('does not add a second one', () => {
    // Caught in the browser: the seeded record was stored as "Dr Asha Rao", and every screen
    // rendered "Dr Dr Asha Rao". Admins type names both ways and always will.
    expect(doctorName('Dr Asha Rao')).toBe('Dr Asha Rao')
    expect(doctorName('Dr. Asha Rao')).toBe('Dr. Asha Rao')
    expect(doctorName('Professor Asha Rao')).toBe('Professor Asha Rao')
  })

  it('is not fooled by a name that merely starts with those letters', () => {
    expect(doctorName('Drew Patel')).toBe('Dr Drew Patel')
  })

  it('trims stray whitespace', () => {
    expect(doctorName('  Asha Rao  ')).toBe('Dr Asha Rao')
  })
})
