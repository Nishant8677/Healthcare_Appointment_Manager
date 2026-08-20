/** Every API call the app makes, in one place.
 *
 * Grouped by portal so it is obvious which role can reach what — and so a component importing
 * `admin.*` is a visible mistake if it lives under `pages/patient`.
 */

import { request } from './client'
import type {
  Appointment,
  Availability,
  AiSummary,
  CalendarAuthorization,
  CalendarConnection,
  CalendarSyncJob,
  CalendarSyncStatus,
  CalendarSyncSummary,
  Doctor,
  DoctorDetail,
  LeaveImpact,
  LeaveRecorded,
  Medication,
  NotificationJob,
  NotificationStatus,
  NotificationSummary,
  RecordVisitResult,
  TokenResponse,
  User,
  WorkingHoursItem,
} from './types'

export const auth = {
  register: (body: { email: string; password: string; full_name: string }) =>
    request<User>('/auth/register', { method: 'POST', body }),

  login: (body: { email: string; password: string }) =>
    request<TokenResponse>('/auth/login', { method: 'POST', body }),

  me: (token: string) => request<User>('/auth/me', { token }),
}

export const doctors = {
  list: (token: string, specialisation?: string) =>
    request<Doctor[]>('/doctors', { token, query: { specialisation } }),

  slots: (token: string, doctorId: string, date: string) =>
    request<Availability>(`/doctors/${doctorId}/slots`, { token, query: { date } }),
}

export const appointments = {
  list: (token: string, includeCancelled = false) =>
    request<Appointment[]>('/appointments', {
      token,
      query: { include_cancelled: includeCancelled },
    }),

  hold: (token: string, body: { doctor_id: string; starts_at: string }) =>
    request<Appointment>('/appointments/hold', { method: 'POST', token, body }),

  confirm: (
    token: string,
    id: string,
    body: { symptoms: string; duration_days?: number | null; additional_notes?: string | null },
  ) => request<Appointment>(`/appointments/${id}/confirm`, { method: 'POST', token, body }),

  cancel: (token: string, id: string, reason?: string) =>
    request<Appointment>(`/appointments/${id}/cancel`, {
      method: 'POST',
      token,
      body: { reason: reason || null },
    }),

  reschedule: (token: string, id: string, startsAt: string) =>
    request<Appointment>(`/appointments/${id}/reschedule`, {
      method: 'POST',
      token,
      body: { starts_at: startsAt },
    }),

  recordVisit: (
    token: string,
    id: string,
    body: { clinical_notes: string; medications: Medication[]; follow_up_date?: string | null },
  ) => request<RecordVisitResult>(`/appointments/${id}/visit`, { method: 'POST', token, body }),

  /** Doctors and admins only — the backend refuses a patient, and no patient route reaches it. */
  preVisitSummary: (token: string, id: string) =>
    request<AiSummary>(`/appointments/${id}/pre-visit-summary`, { token }),

  postVisitSummary: (token: string, id: string) =>
    request<AiSummary>(`/appointments/${id}/post-visit-summary`, { token }),
}

export const calendar = {
  connection: (token: string) => request<CalendarConnection>('/calendar/connection', { token }),

  connect: (token: string) =>
    request<CalendarAuthorization>('/calendar/connect', { method: 'POST', token }),

  disconnect: (token: string) =>
    request<CalendarConnection>('/calendar/connection', { method: 'DELETE', token }),
}

export const admin = {
  listDoctors: (token: string, includeInactive = false) =>
    request<Doctor[]>('/admin/doctors', { token, query: { include_inactive: includeInactive } }),

  getDoctor: (token: string, id: string) =>
    request<DoctorDetail>(`/admin/doctors/${id}`, { token }),

  createDoctor: (
    token: string,
    body: {
      email: string
      password: string
      full_name: string
      specialisation: string
      slot_duration_minutes: number
      working_hours?: WorkingHoursItem[]
    },
  ) => request<DoctorDetail>('/admin/doctors', { method: 'POST', token, body }),

  updateDoctor: (
    token: string,
    id: string,
    body: {
      specialisation?: string
      slot_duration_minutes?: number
      full_name?: string
      is_active?: boolean
    },
  ) => request<DoctorDetail>(`/admin/doctors/${id}`, { method: 'PATCH', token, body }),

  replaceWorkingHours: (token: string, id: string, working_hours: WorkingHoursItem[]) =>
    request<DoctorDetail>(`/admin/doctors/${id}/working-hours`, {
      method: 'PUT',
      token,
      body: { working_hours },
    }),

  /** Read-only preview of whose appointments a leave day would cancel. Changes nothing. */
  leaveImpact: (token: string, id: string, date: string) =>
    request<LeaveImpact>(`/admin/doctors/${id}/leave/impact`, { token, query: { date } }),

  recordLeave: (
    token: string,
    id: string,
    body: { leave_date: string; reason?: string | null; cancel_existing_appointments?: boolean },
  ) => request<LeaveRecorded>(`/admin/doctors/${id}/leave`, { method: 'POST', token, body }),

  removeLeave: (token: string, doctorId: string, leaveId: string) =>
    request<void>(`/admin/doctors/${doctorId}/leave/${leaveId}`, { method: 'DELETE', token }),

  notifications: (token: string, status?: NotificationStatus) =>
    request<NotificationJob[]>('/admin/notifications', { token, query: { status } }),

  notificationSummary: (token: string) =>
    request<NotificationSummary>('/admin/notifications/summary', { token }),

  retryNotification: (token: string, id: string) =>
    request<NotificationJob>(`/admin/notifications/${id}/retry`, { method: 'POST', token }),

  calendarJobs: (token: string, status?: CalendarSyncStatus) =>
    request<CalendarSyncJob[]>('/admin/calendar/sync-jobs', { token, query: { status } }),

  calendarSummary: (token: string) =>
    request<CalendarSyncSummary>('/admin/calendar/summary', { token }),

  retryCalendarJob: (token: string, id: string) =>
    request<CalendarSyncJob>(`/admin/calendar/sync-jobs/${id}/retry`, { method: 'POST', token }),
}
