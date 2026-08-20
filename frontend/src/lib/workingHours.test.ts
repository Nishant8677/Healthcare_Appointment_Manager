import { describe, expect, it } from 'vitest'

import { emptyWeek, toWorkingHours, weekFromWorkingHours } from './workingHours'

describe('weekFromWorkingHours', () => {
  it('trims the seconds the API sends', () => {
    // `<input type="time">` silently renders empty for "09:00:00", which looks to an admin
    // like a doctor with no hours set — and saving from that state would wipe the schedule.
    const week = weekFromWorkingHours([
      { weekday: 0, start_time: '09:00:00', end_time: '17:00:00' },
    ])
    expect(week[0].start_time).toBe('09:00')
    expect(week[0].end_time).toBe('17:00')
  })

  it('marks days without hours as not working', () => {
    const week = weekFromWorkingHours([
      { weekday: 2, start_time: '10:00:00', end_time: '13:00:00' },
    ])
    expect(week.filter((day) => day.enabled).map((day) => day.weekday)).toEqual([2])
  })

  it('always returns all seven days, so every one can be switched on', () => {
    expect(weekFromWorkingHours([])).toHaveLength(7)
  })
})

describe('toWorkingHours', () => {
  it('drops the days that are switched off', () => {
    const week = weekFromWorkingHours([
      { weekday: 0, start_time: '09:00:00', end_time: '17:00:00' },
      { weekday: 4, start_time: '09:00:00', end_time: '12:00:00' },
    ])
    expect(toWorkingHours(week)).toEqual([
      { weekday: 0, start_time: '09:00', end_time: '17:00' },
      { weekday: 4, start_time: '09:00', end_time: '12:00' },
    ])
  })

  it('round-trips a schedule unchanged apart from the seconds', () => {
    const original = [
      { weekday: 1, start_time: '08:30:00', end_time: '16:30:00' },
      { weekday: 3, start_time: '11:00:00', end_time: '15:00:00' },
    ]
    expect(toWorkingHours(weekFromWorkingHours(original))).toEqual([
      { weekday: 1, start_time: '08:30', end_time: '16:30' },
      { weekday: 3, start_time: '11:00', end_time: '15:00' },
    ])
  })
})

describe('emptyWeek', () => {
  it('defaults to a Monday-to-Friday week', () => {
    expect(emptyWeek().filter((day) => day.enabled).map((day) => day.weekday)).toEqual([
      0, 1, 2, 3, 4,
    ])
  })
})
