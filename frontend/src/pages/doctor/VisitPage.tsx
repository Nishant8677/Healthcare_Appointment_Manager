import { useState, type FormEvent, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'

import { ApiError } from '../../api/client'
import { appointments as api } from '../../api/endpoints'
import type { Medication } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { PreVisitSummaryPanel } from '../../components/SummaryPanel'
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
  TextArea,
  TextInput,
} from '../../components/ui'
import { useAppointment } from '../../hooks/useAppointment'
import { useAction, useResource } from '../../hooks/useResource'
import { appointmentStatus, formatDateTime, formatTime } from '../../lib/format'
import './Visit.css'

type MedicationDraft = Medication & { key: string }

function emptyMedication(): MedicationDraft {
  return {
    key: crypto.randomUUID(),
    drug_name: '',
    dosage: '',
    times_per_day: 2,
    duration_days: 5,
    instructions: '',
  }
}

export function VisitPage(): ReactNode {
  const token = useToken()
  const { appointmentId = '' } = useParams()

  const resource = useAppointment(token, appointmentId)
  const appointment = resource.data

  const summary = useResource(async () => {
    try {
      return await api.preVisitSummary(token, appointmentId)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  }, [token, appointmentId])

  const [notes, setNotes] = useState('')
  const [followUp, setFollowUp] = useState('')
  const [medications, setMedications] = useState<MedicationDraft[]>([])

  const record = useAction(async () => {
    const result = await api.recordVisit(token, appointmentId, {
      clinical_notes: notes.trim(),
      // The structured fields are what drive the reminder schedule — never the prose.
      medications: medications
        .filter((item) => item.drug_name.trim() && item.dosage.trim())
        .map(({ key: _key, ...rest }) => ({
          ...rest,
          instructions: rest.instructions?.trim() || null,
        })),
      follow_up_date: followUp || null,
    })
    resource.reload()
    return result
  })

  function updateMedication(key: string, patch: Partial<Medication>): void {
    setMedications((current) =>
      current.map((item) => (item.key === key ? { ...item, ...patch } : item)),
    )
  }

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault()
    await record.run()
  }

  if (resource.loading) return <Spinner label="Loading the appointment" />
  if (resource.error !== null) return <Alert title="Could not load this">{resource.error}</Alert>
  if (appointment === null) return <EmptyState title="Appointment not found" />

  const status = appointmentStatus(appointment.status, false)
  const alreadyRecorded = appointment.status === 'completed'

  return (
    <>
      <PageHeader
        title={appointment.patient.full_name}
        description={`${formatDateTime(appointment.starts_at)} — ${formatTime(appointment.ends_at)}`}
        actions={<LinkButton to="/doctor/schedule">Back to schedule</LinkButton>}
      />

      <div style={{ marginBottom: '1rem' }}>
        <Pill tone={status.tone}>{status.label}</Pill>
      </div>

      <div className="grid-2">
        <div className="stack">
          <Card>
            <CardHeader
              title="What the patient described"
              subtitle="Their own words, recorded when they booked."
            />
            {appointment.symptom_report ? (
              <>
                <p className="summary__prose">{appointment.symptom_report.symptoms}</p>
                {appointment.symptom_report.duration_days !== null && (
                  <p className="summary__meta">
                    Duration: {appointment.symptom_report.duration_days} day
                    {appointment.symptom_report.duration_days === 1 ? '' : 's'}
                  </p>
                )}
                {appointment.symptom_report.additional_notes && (
                  <>
                    <h3 className="summary__heading">Also mentioned</h3>
                    <p className="summary__prose">
                      {appointment.symptom_report.additional_notes}
                    </p>
                  </>
                )}
              </>
            ) : (
              <p className="summary__muted">No symptom form was submitted.</p>
            )}
          </Card>

          <PreVisitSummaryPanel
            summary={summary.data}
            loading={summary.loading}
            error={summary.error}
          />
        </div>

        <Card>
          <CardHeader
            title="Record the visit"
            subtitle="Your notes become the clinical record; the patient gets a plain-language version."
          />

          {alreadyRecorded ? (
            <Alert tone="success" title="This visit has been recorded">
              <p>
                The patient's summary is being prepared and their medication reminders are
                queued.
              </p>
            </Alert>
          ) : (
            <form onSubmit={onSubmit} noValidate>
              {record.error && (
                <div style={{ marginBottom: '1rem' }}>
                  <Alert title="Could not save">{record.error}</Alert>
                </div>
              )}

              <Field
                label="Clinical notes"
                htmlFor="notes"
                error={record.fieldErrors.clinical_notes}
                hint="Findings, diagnosis and advice. Written for the record, not for the patient."
              >
                <TextArea
                  id="notes"
                  value={notes}
                  required
                  invalid={Boolean(record.fieldErrors.clinical_notes)}
                  onChange={(event) => setNotes(event.target.value)}
                />
              </Field>

              <fieldset className="meds">
                <legend className="field__label">Prescription</legend>
                <p className="field__hint meds__note">
                  Dose and duration here drive the patient's reminders directly, so they are
                  typed rather than written into the notes.
                </p>

                {medications.length === 0 && (
                  <p className="summary__muted">No medicines prescribed.</p>
                )}

                {medications.map((medication, index) => (
                  <div className="med" key={medication.key}>
                    <div className="med__head">
                      <span className="med__index">Medicine {index + 1}</span>
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setMedications((current) =>
                            current.filter((item) => item.key !== medication.key),
                          )
                        }
                      >
                        Remove
                      </Button>
                    </div>

                    <div className="med__grid">
                      <Field label="Name" htmlFor={`drug-${medication.key}`}>
                        <TextInput
                          id={`drug-${medication.key}`}
                          value={medication.drug_name}
                          placeholder="Amoxicillin"
                          onChange={(event) =>
                            updateMedication(medication.key, { drug_name: event.target.value })
                          }
                        />
                      </Field>

                      <Field label="Dose" htmlFor={`dose-${medication.key}`}>
                        <TextInput
                          id={`dose-${medication.key}`}
                          value={medication.dosage}
                          placeholder="500 mg"
                          onChange={(event) =>
                            updateMedication(medication.key, { dosage: event.target.value })
                          }
                        />
                      </Field>

                      <Field label="Times a day" htmlFor={`freq-${medication.key}`}>
                        <TextInput
                          id={`freq-${medication.key}`}
                          type="number"
                          min={1}
                          max={12}
                          value={medication.times_per_day}
                          onChange={(event) =>
                            updateMedication(medication.key, {
                              times_per_day: Number(event.target.value),
                            })
                          }
                        />
                      </Field>

                      <Field label="For how many days" htmlFor={`days-${medication.key}`}>
                        <TextInput
                          id={`days-${medication.key}`}
                          type="number"
                          min={1}
                          max={365}
                          value={medication.duration_days}
                          onChange={(event) =>
                            updateMedication(medication.key, {
                              duration_days: Number(event.target.value),
                            })
                          }
                        />
                      </Field>
                    </div>

                    <Field label="Instructions" htmlFor={`inst-${medication.key}`}>
                      <TextInput
                        id={`inst-${medication.key}`}
                        value={medication.instructions ?? ''}
                        placeholder="After food"
                        onChange={(event) =>
                          updateMedication(medication.key, { instructions: event.target.value })
                        }
                      />
                    </Field>
                  </div>
                ))}

                <Button
                  variant="secondary"
                  onClick={() => setMedications((current) => [...current, emptyMedication()])}
                >
                  Add a medicine
                </Button>
              </fieldset>

              <Field
                label="Follow-up date"
                htmlFor="follow-up"
                hint="Optional. Included in the patient's summary."
              >
                <TextInput
                  id="follow-up"
                  type="date"
                  value={followUp}
                  onChange={(event) => setFollowUp(event.target.value)}
                />
              </Field>

              <div className="symptom__footer">
                <Button
                  type="submit"
                  variant="primary"
                  loading={record.pending}
                  disabled={notes.trim().length === 0}
                >
                  Complete visit
                </Button>
              </div>
            </form>
          )}
        </Card>
      </div>
    </>
  )
}
