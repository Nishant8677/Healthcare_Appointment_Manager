/** One appointment, as it appears in a list. Used by both the patient's and the doctor's
 * screens — the difference is whose name is shown and where it links. */

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import type { Appointment } from '../api/types'
import {
  appointmentStatus,
  doctorName,
  formatDayAndDate,
  formatTime,
  isCancelled,
} from '../lib/format'
import { Pill } from './ui'
import './AppointmentCard.css'

export function AppointmentCard({
  appointment,
  viewer,
  to,
  actions,
}: {
  appointment: Appointment
  viewer: 'patient' | 'doctor'
  to?: string
  actions?: ReactNode
}): ReactNode {
  const status = appointmentStatus(appointment.status, viewer === 'patient')
  const who =
    viewer === 'patient'
      ? doctorName(appointment.doctor.full_name)
      : appointment.patient.full_name
  const detail =
    viewer === 'patient' ? appointment.doctor.specialisation : 'Patient'

  return (
    <article className={`appt ${isCancelled(appointment.status) ? 'appt--muted' : ''}`}>
      <div className="appt__when">
        <span className="appt__time">{formatTime(appointment.starts_at)}</span>
        <span className="appt__date">{formatDayAndDate(appointment.starts_at)}</span>
      </div>

      <div className="appt__who">
        <p className="appt__name">{to ? <Link to={to}>{who}</Link> : who}</p>
        <p className="appt__detail">{detail}</p>
        {appointment.cancellation_reason && (
          <p className="appt__reason">{appointment.cancellation_reason}</p>
        )}
      </div>

      <div className="appt__side">
        <Pill tone={status.tone}>{status.label}</Pill>
        {actions && <div className="appt__actions">{actions}</div>}
      </div>
    </article>
  )
}
