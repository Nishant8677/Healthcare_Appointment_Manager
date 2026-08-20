import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { admin as api } from '../../api/endpoints'
import { useToken } from '../../auth/useAuth'
import { Button, DataState, EmptyState, LinkButton, PageHeader, Pill } from '../../components/ui'
import { useResource } from '../../hooks/useResource'
import { doctorName } from '../../lib/format'

export function DoctorsPage(): ReactNode {
  const token = useToken()
  const [includeInactive, setIncludeInactive] = useState(false)
  const resource = useResource(() => api.listDoctors(token, includeInactive), [token, includeInactive])

  return (
    <>
      <PageHeader
        title="Doctors"
        description="Create accounts, set working hours and record leave."
        actions={
          <>
            <Button variant="ghost" onClick={() => setIncludeInactive((value) => !value)}>
              {includeInactive ? 'Hide inactive' : 'Show inactive'}
            </Button>
            <LinkButton to="/admin/doctors/new" variant="primary">
              Add a doctor
            </LinkButton>
          </>
        }
      />

      <DataState
        resource={resource}
        empty={
          <EmptyState
            title="No doctors yet"
            action={
              <LinkButton to="/admin/doctors/new" variant="primary">
                Add the first doctor
              </LinkButton>
            }
          >
            Patients cannot book until at least one doctor has working hours.
          </EmptyState>
        }
      >
        {(doctorList) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Specialisation</th>
                  <th>Email</th>
                  <th>Slot</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {doctorList.map((doctor) => (
                  <tr key={doctor.id}>
                    <td>
                      <Link to={`/admin/doctors/${doctor.id}`}>{doctorName(doctor.full_name)}</Link>
                    </td>
                    <td>{doctor.specialisation}</td>
                    <td className="mono">{doctor.email}</td>
                    <td>{doctor.slot_duration_minutes} min</td>
                    <td>
                      {doctor.is_active ? (
                        <Pill tone="success">Active</Pill>
                      ) : (
                        <Pill tone="neutral">Inactive</Pill>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataState>
    </>
  )
}
