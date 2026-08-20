import { useMemo, useState, type ReactNode } from 'react'

import { doctors as api } from '../../api/endpoints'
import { useToken } from '../../auth/useAuth'
import { DataState, EmptyState, LinkButton, PageHeader, Select } from '../../components/ui'
import { useResource } from '../../hooks/useResource'
import { doctorName } from '../../lib/format'
import './FindDoctor.css'

/** Filtered client-side from the full list rather than by a second request per keystroke.
 * A clinic has tens of doctors, not thousands; fetching once and narrowing locally is both
 * faster to use and less load on the API than a debounced search endpoint would be. */
export function FindDoctorPage(): ReactNode {
  const token = useToken()
  const [specialisation, setSpecialisation] = useState('')
  const [query, setQuery] = useState('')

  const resource = useResource(() => api.list(token), [token])

  const specialisations = useMemo(() => {
    const unique = new Set((resource.data ?? []).map((doctor) => doctor.specialisation))
    return [...unique].sort((a, b) => a.localeCompare(b))
  }, [resource.data])

  const visible = (resource.data ?? []).filter((doctor) => {
    if (specialisation && doctor.specialisation !== specialisation) return false
    if (!query.trim()) return true
    const haystack = `${doctor.full_name} ${doctor.specialisation}`.toLowerCase()
    return haystack.includes(query.trim().toLowerCase())
  })

  return (
    <>
      <PageHeader
        title="Book an appointment"
        description="Choose a doctor, then pick a time that suits you."
      />

      <div className="finder">
        <div className="finder__field">
          <label className="field__label" htmlFor="specialisation">
            Specialisation
          </label>
          <Select
            id="specialisation"
            value={specialisation}
            onChange={(event) => setSpecialisation(event.target.value)}
          >
            <option value="">All specialisations</option>
            {specialisations.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </div>

        <div className="finder__field">
          <label className="field__label" htmlFor="search">
            Search by name
          </label>
          <input
            id="search"
            type="search"
            className="input"
            placeholder="e.g. Rao"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </div>

      <DataState
        resource={resource}
        empty={<EmptyState title="No doctors are taking appointments yet" />}
      >
        {() =>
          visible.length === 0 ? (
            <EmptyState title="No doctors match that">
              Try clearing the filters above.
            </EmptyState>
          ) : (
            <ul className="doctors">
              {visible.map((doctor) => (
                <li key={doctor.id} className="doctor">
                  <div className="doctor__avatar" aria-hidden="true">
                    {initials(doctor.full_name)}
                  </div>
                  <div className="doctor__body">
                    <p className="doctor__name">{doctorName(doctor.full_name)}</p>
                    <p className="doctor__meta">
                      {doctor.specialisation} · {doctor.slot_duration_minutes}-minute
                      appointments
                    </p>
                  </div>
                  <LinkButton to={`/book/${doctor.id}`} variant="primary">
                    See times
                  </LinkButton>
                </li>
              ))}
            </ul>
          )
        }
      </DataState>
    </>
  )
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
}
