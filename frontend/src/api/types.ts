/** The API's contract, transcribed from the backend's OpenAPI schema.
 *
 * Hand-written rather than code-generated. A generator would be another build step and another
 * dependency for roughly two hundred lines that change once a phase; more importantly, writing
 * them by hand forces a read of the actual contract, which is how the `hold_expires_at`
 * nullability and the two distinct cancellation statuses got noticed rather than assumed.
 */

export type UserRole = 'patient' | 'doctor' | 'admin'

export type AppointmentStatus =
  | 'held'
  | 'confirmed'
  | 'completed'
  | 'cancelled_by_patient'
  | 'cancelled_by_clinic'

export type SummaryType = 'pre_visit' | 'post_visit'
export type SummaryStatus = 'pending' | 'ready' | 'failed'
export type UrgencyLevel = 'low' | 'medium' | 'high'

export type NotificationType =
  | 'booking_confirmation'
  | 'appointment_reminder'
  | 'cancellation'
  | 'leave_conflict'
  | 'medication_reminder'

export type NotificationStatus = 'pending' | 'sent' | 'failed'

export type CalendarSyncAction = 'sync' | 'delete'
export type CalendarSyncStatus = 'pending' | 'synced' | 'skipped' | 'failed'

/** Statuses that still occupy a slot — kept in step with the backend's OCCUPYING_STATUSES. */
export const ACTIVE_STATUSES: readonly AppointmentStatus[] = ['held', 'confirmed', 'completed']

export interface User {
  id: string
  email: string
  full_name: string
  role: UserRole
  is_active: boolean
  created_at: string
}

export interface TokenResponse {
  access_token: string
  token_type?: string
  expires_in: number
}

export interface DoctorSummary {
  id: string
  full_name: string
  specialisation: string
  slot_duration_minutes: number
}

export interface Doctor extends DoctorSummary {
  user_id: string
  email: string
  is_active: boolean
}

export interface WorkingHours {
  id: string
  weekday: number
  start_time: string
  end_time: string
}

export interface WorkingHoursItem {
  weekday: number
  start_time: string
  end_time: string
}

export interface LeaveDay {
  id: string
  leave_date: string
  reason: string | null
}

export interface DoctorDetail extends Doctor {
  working_hours: WorkingHours[]
  leave_days: LeaveDay[]
}

export interface Slot {
  starts_at: string
  ends_at: string
}

export interface Availability {
  doctor_id: string
  date: string
  slot_duration_minutes: number
  timezone: string
  slots: Slot[]
}

export interface SymptomReport {
  symptoms: string
  duration_days: number | null
  additional_notes: string | null
}

export interface PatientSummary {
  id: string
  full_name: string
}

export interface Appointment {
  id: string
  status: AppointmentStatus
  starts_at: string
  ends_at: string
  doctor: DoctorSummary
  patient: PatientSummary
  /** Only set while the slot is held; the countdown on the symptom form runs off this. */
  hold_expires_at?: string | null
  cancellation_reason?: string | null
  symptom_report?: SymptomReport | null
}

/** What the model produced for the doctor, before the visit. */
export interface PreVisitContent {
  urgency: string
  chief_complaint: string
  suggested_questions: string[]
}

/** What the model produced for the patient, after it. */
export interface PostVisitContent {
  summary: string
  medication_schedule: string[]
  follow_up_steps: string[]
}

export interface AiSummary {
  summary_type: SummaryType
  status: SummaryStatus
  urgency?: UrgencyLevel | null
  content?: PreVisitContent | PostVisitContent | null
  prompt_version: string
  model?: string | null
  attempts: number
  /** Set when the summary is not `ready`; says whether it is late or will never arrive. */
  unavailable_reason?: string | null
}

export interface Medication {
  drug_name: string
  dosage: string
  times_per_day: number
  duration_days: number
  instructions?: string | null
}

export interface RecordVisitResult {
  appointment_id: string
  status: string
  completed_at: string
  reminders_scheduled: number
}

export interface AffectedAppointment {
  appointment_id: string
  patient_name: string
  patient_email: string
  starts_at: string
  status: string
}

export interface LeaveImpact {
  doctor_id: string
  leave_date: string
  affected_count: number
  appointments: AffectedAppointment[]
}

export interface LeaveRecorded {
  id: string
  leave_date: string
  reason: string | null
  cancelled_appointments: number
  patients_notified: number
}

export interface NotificationJob {
  id: string
  notification_type: NotificationType
  status: NotificationStatus
  recipient_email: string
  appointment_id: string | null
  scheduled_for: string
  attempts: number
  next_attempt_at: string | null
  sent_at: string | null
  last_error?: string | null
}

export interface NotificationSummary {
  pending: number
  sent: number
  failed: number
}

export interface CalendarConnection {
  connected: boolean
  google_account_email?: string | null
  calendar_id?: string | null
  connected_at?: string | null
  revoked_at?: string | null
  last_error?: string | null
}

export interface CalendarAuthorization {
  authorization_url: string
  expires_in_minutes: number
}

export interface CalendarSyncJob {
  id: string
  appointment_id: string
  user_id: string
  action: CalendarSyncAction
  status: CalendarSyncStatus
  google_event_id: string
  calendar_id: string
  attempts: number
  next_attempt_at: string | null
  synced_at: string | null
  last_error: string | null
}

export interface CalendarSyncSummary {
  pending: number
  synced: number
  skipped: number
  failed: number
}
