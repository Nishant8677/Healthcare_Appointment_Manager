import { useState, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'

import { admin as api } from '../../api/endpoints'
import type { DoctorDetail } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { WorkingHoursEditor } from '../../components/WorkingHoursEditor'
import {
  Alert,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Field,
  LinkButton,
  PageHeader,
  Pill,
  Spinner,
  TextInput,
} from '../../components/ui'
import { useAction, useResource } from '../../hooks/useResource'
import { doctorName, formatDate, formatDateTime, toDateInputValue } from '../../lib/format'
import { toWorkingHours, weekFromWorkingHours, type DayDraft } from '../../lib/workingHours'
import './DoctorDetail.css'

export function DoctorDetailPage(): ReactNode {
  const token = useToken()
  const { doctorId = '' } = useParams()

  const resource = useResource(() => api.getDoctor(token, doctorId), [token, doctorId])
  const doctor = resource.data

  const setActive = useAction(async (isActive: boolean) => {
    await api.updateDoctor(token, doctorId, { is_active: isActive })
    resource.reload()
  })

  if (resource.loading && doctor === null) return <Spinner label="Loading the doctor" />
  if (resource.error !== null) return <Alert title="Could not load this">{resource.error}</Alert>
  if (doctor === null) return <EmptyState title="Doctor not found" />

  return (
    <>
      <PageHeader
        title={doctorName(doctor.full_name)}
        description={doctor.email}
        actions={
          <>
            <LinkButton to="/admin/doctors">All doctors</LinkButton>
            <Button
              variant={doctor.is_active ? 'danger' : 'primary'}
              loading={setActive.pending}
              onClick={() => setActive.run(!doctor.is_active)}
            >
              {doctor.is_active ? 'Deactivate' : 'Reactivate'}
            </Button>
          </>
        }
      />

      {!doctor.is_active && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert tone="warning" title="This doctor is inactive">
            They do not appear in patient searches and cannot take new bookings.
          </Alert>
        </div>
      )}

      <div className="stack">
        {/* Keyed on the doctor's id so React resets the form when the route changes to a
            different doctor, and *keeps* it across a reload of the same one. Seeding this
            state from an effect instead would clobber an admin's unsaved edits every time
            something else on the page triggered a refetch. */}
        <DoctorSettings
          key={doctor.id}
          doctor={doctor}
          token={token}
          onSaved={() => resource.reload()}
        />

        <LeaveCard
          doctor={doctor}
          token={token}
          onChanged={() => resource.reload()}
        />
      </div>
    </>
  )
}

function DoctorSettings({
  doctor,
  token,
  onSaved,
}: {
  doctor: DoctorDetail
  token: string
  onSaved: () => void
}): ReactNode {
  const [specialisation, setSpecialisation] = useState(doctor.specialisation)
  const [slotMinutes, setSlotMinutes] = useState(doctor.slot_duration_minutes)
  const [week, setWeek] = useState<DayDraft[]>(() => weekFromWorkingHours(doctor.working_hours))

  const saveProfile = useAction(async () => {
    await api.updateDoctor(token, doctor.id, {
      specialisation: specialisation.trim(),
      slot_duration_minutes: slotMinutes,
    })
    onSaved()
  })

  const saveHours = useAction(async () => {
    await api.replaceWorkingHours(token, doctor.id, toWorkingHours(week))
    onSaved()
  })

  return (
    <>
      <Card>
        <CardHeader
          title="Profile"
          actions={
            <Button variant="primary" loading={saveProfile.pending} onClick={() => saveProfile.run()}>
              Save
            </Button>
          }
        />

        {saveProfile.error && (
          <div style={{ marginBottom: '1rem' }}>
            <Alert title="Could not save">{saveProfile.error}</Alert>
          </div>
        )}

        <div className="two-up">
          <Field label="Specialisation" htmlFor="specialisation">
            <TextInput
              id="specialisation"
              value={specialisation}
              onChange={(event) => setSpecialisation(event.target.value)}
            />
          </Field>

          <Field
            label="Appointment length (minutes)"
            htmlFor="slot"
            hint="Changing this re-checks that every working day still divides evenly."
          >
            <TextInput
              id="slot"
              type="number"
              min={5}
              max={240}
              step={5}
              value={slotMinutes}
              onChange={(event) => setSlotMinutes(Number(event.target.value))}
            />
          </Field>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Working hours"
          subtitle="Replaces the whole week. Each day must divide exactly into appointments."
          actions={
            <Button variant="primary" loading={saveHours.pending} onClick={() => saveHours.run()}>
              Save hours
            </Button>
          }
        />

        {saveHours.error && (
          <div style={{ marginBottom: '1rem' }}>
            <Alert title="Could not save these hours">{saveHours.error}</Alert>
          </div>
        )}

        <WorkingHoursEditor week={week} onChange={setWeek} />
      </Card>
    </>
  )
}

/** Recording leave, with the cost shown before it is paid.
 *
 * The impact preview loads on every date change because the endpoint changes nothing. Seeing
 * exactly whose appointments would be cancelled — by name and time — before pressing anything
 * is the entire point: cancelling several people's medical appointments should never be a
 * side effect of picking a date.
 */
function LeaveCard({
  doctor,
  token,
  onChanged,
}: {
  doctor: DoctorDetail
  token: string
  onChanged: () => void
}): ReactNode {
  const [leaveDate, setLeaveDate] = useState(() => toDateInputValue(new Date()))
  const [leaveReason, setLeaveReason] = useState('')
  const [recorded, setRecorded] = useState<string | null>(null)

  const impact = useResource(
    async () => (leaveDate ? api.leaveImpact(token, doctor.id, leaveDate) : null),
    [token, doctor.id, leaveDate],
  )

  const recordLeave = useAction(async (cancelExisting: boolean) => {
    const result = await api.recordLeave(token, doctor.id, {
      leave_date: leaveDate,
      reason: leaveReason.trim() || null,
      cancel_existing_appointments: cancelExisting,
    })
    setRecorded(
      result.cancelled_appointments > 0
        ? `${formatDate(result.leave_date)} recorded. ${result.cancelled_appointments} appointment(s) cancelled, ${result.patients_notified} patient(s) notified.`
        : `${formatDate(result.leave_date)} recorded as leave.`,
    )
    setLeaveReason('')
    onChanged()
    impact.reload()
  })

  const removeLeave = useAction(async (leaveId: string) => {
    await api.removeLeave(token, doctor.id, leaveId)
    onChanged()
    impact.reload()
  })

  const affected = impact.data?.affected_count ?? 0

  return (
    <Card>
      <CardHeader
        title="Leave"
        subtitle="Days this doctor is away. Booked patients are cancelled and notified."
      />

      {recorded && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert tone="success" title="Leave recorded">
            {recorded}
          </Alert>
        </div>
      )}

      {recordLeave.error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert title="Could not record leave">{recordLeave.error}</Alert>
        </div>
      )}

      <div className="two-up">
        <Field label="Date" htmlFor="leave-date">
          <TextInput
            id="leave-date"
            type="date"
            value={leaveDate}
            min={toDateInputValue(new Date())}
            onChange={(event) => {
              setLeaveDate(event.target.value)
              setRecorded(null)
            }}
          />
        </Field>

        <Field label="Reason" htmlFor="leave-reason" hint="Optional. Internal only.">
          <TextInput
            id="leave-reason"
            value={leaveReason}
            placeholder="Conference"
            onChange={(event) => setLeaveReason(event.target.value)}
          />
        </Field>
      </div>

      {impact.loading ? (
        <Spinner label="Checking who would be affected" />
      ) : affected > 0 ? (
        <div className="impact">
          <Alert
            tone="warning"
            title={`${affected} appointment${affected === 1 ? '' : 's'} already booked`}
          >
            <p>
              Recording this leave will cancel {affected === 1 ? 'it' : 'them'} and email
              {affected === 1 ? ' the patient' : ' each patient'}. This cannot be undone.
            </p>
          </Alert>

          <ul className="impact__list">
            {impact.data?.appointments.map((item) => (
              <li key={item.appointment_id}>
                <span className="impact__name">{item.patient_name}</span>
                <span className="impact__time">{formatDateTime(item.starts_at)}</span>
                <span className="impact__email mono">{item.patient_email}</span>
              </li>
            ))}
          </ul>

          <Button
            variant="danger"
            loading={recordLeave.pending}
            onClick={() => recordLeave.run(true)}
          >
            Cancel {affected} appointment{affected === 1 ? '' : 's'} and record leave
          </Button>
        </div>
      ) : (
        <>
          <p className="summary__muted">
            No appointments are booked on this date — nobody will be affected.
          </p>
          <div className="symptom__footer">
            <Button
              variant="primary"
              loading={recordLeave.pending}
              onClick={() => recordLeave.run(false)}
            >
              Record leave
            </Button>
          </div>
        </>
      )}

      {doctor.leave_days.length > 0 && (
        <>
          <h3 className="summary__heading">Recorded leave</h3>
          <ul className="leave-days">
            {doctor.leave_days.map((day) => (
              <li key={day.id}>
                <Pill tone="neutral">{formatDate(day.leave_date)}</Pill>
                {day.reason && <span className="leave-days__reason">{day.reason}</span>}
                <Button
                  variant="ghost"
                  loading={removeLeave.pending}
                  onClick={() => removeLeave.run(day.id)}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  )
}
