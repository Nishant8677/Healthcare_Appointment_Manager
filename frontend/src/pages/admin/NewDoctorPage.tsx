import { useState, type FormEvent, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

import { admin as api } from '../../api/endpoints'
import { useToken } from '../../auth/useAuth'
import { WorkingHoursEditor } from '../../components/WorkingHoursEditor'
import {
  Alert,
  Button,
  Card,
  CardHeader,
  Field,
  LinkButton,
  PageHeader,
  TextInput,
} from '../../components/ui'
import { useAction } from '../../hooks/useResource'
import { emptyWeek, toWorkingHours, type DayDraft } from '../../lib/workingHours'

export function NewDoctorPage(): ReactNode {
  const token = useToken()
  const navigate = useNavigate()

  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [specialisation, setSpecialisation] = useState('')
  const [slotMinutes, setSlotMinutes] = useState(30)
  const [week, setWeek] = useState<DayDraft[]>(emptyWeek)

  const create = useAction(async () => {
    const created = await api.createDoctor(token, {
      email: email.trim(),
      password,
      full_name: fullName.trim(),
      specialisation: specialisation.trim(),
      slot_duration_minutes: slotMinutes,
      working_hours: toWorkingHours(week),
    })
    navigate(`/admin/doctors/${created.id}`, { replace: true })
  })

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault()
    await create.run()
  }

  return (
    <>
      <PageHeader
        title="Add a doctor"
        description="Creates their sign-in and their clinic profile together."
        actions={<LinkButton to="/admin/doctors">Cancel</LinkButton>}
      />

      <form onSubmit={onSubmit} noValidate className="stack">
        {create.error && <Alert title="Could not create this doctor">{create.error}</Alert>}

        <Card>
          <CardHeader title="Account" subtitle="The doctor signs in with these." />

          <Field label="Full name" htmlFor="full_name" error={create.fieldErrors.full_name}>
            <TextInput
              id="full_name"
              value={fullName}
              required
              placeholder="Asha Rao"
              invalid={Boolean(create.fieldErrors.full_name)}
              onChange={(event) => setFullName(event.target.value)}
            />
          </Field>

          <Field label="Email" htmlFor="email" error={create.fieldErrors.email}>
            <TextInput
              id="email"
              type="email"
              value={email}
              required
              invalid={Boolean(create.fieldErrors.email)}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>

          <Field
            label="Temporary password"
            htmlFor="password"
            error={create.fieldErrors.password}
            hint="Give this to the doctor directly. At least 10 characters."
          >
            <TextInput
              id="password"
              type="text"
              value={password}
              required
              minLength={10}
              invalid={Boolean(create.fieldErrors.password)}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
        </Card>

        <Card>
          <CardHeader title="Clinic profile" />

          <Field
            label="Specialisation"
            htmlFor="specialisation"
            error={create.fieldErrors.specialisation}
            hint="Patients search by this."
          >
            <TextInput
              id="specialisation"
              value={specialisation}
              required
              placeholder="Cardiology"
              invalid={Boolean(create.fieldErrors.specialisation)}
              onChange={(event) => setSpecialisation(event.target.value)}
            />
          </Field>

          <Field
            label="Appointment length (minutes)"
            htmlFor="slot"
            error={create.fieldErrors.slot_duration_minutes}
            hint="Working hours must divide exactly into this."
          >
            <TextInput
              id="slot"
              type="number"
              min={5}
              max={240}
              step={5}
              value={slotMinutes}
              invalid={Boolean(create.fieldErrors.slot_duration_minutes)}
              onChange={(event) => setSlotMinutes(Number(event.target.value))}
            />
          </Field>
        </Card>

        <Card>
          <CardHeader
            title="Working hours"
            subtitle="Untick a day to mark it as not working. These can be changed later."
          />
          <WorkingHoursEditor week={week} onChange={setWeek} />
        </Card>

        <div className="symptom__footer">
          <Button type="submit" variant="primary" loading={create.pending}>
            Create doctor
          </Button>
        </div>
      </form>
    </>
  )
}
