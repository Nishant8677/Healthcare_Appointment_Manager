import { useState, type ReactNode } from 'react'

import { admin as api } from '../../api/endpoints'
import type { CalendarSyncStatus } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { Alert, Button, DataState, EmptyState, PageHeader, Pill, Select } from '../../components/ui'
import { useAction, useResource } from '../../hooks/useResource'
import { calendarStatus, formatDateTime, humanise } from '../../lib/format'
import { Stat } from './NotificationsPage'
import './Operations.css'

const FILTERS: Array<{ value: '' | CalendarSyncStatus; label: string }> = [
  { value: '', label: 'All entries' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Queued' },
  { value: 'synced', label: 'In sync' },
  { value: 'skipped', label: 'No calendar' },
]

/** Calendar sync state.
 *
 * The number worth watching is `failed`. `skipped` means the person has no calendar
 * connected, which is the normal state for most patients — counting it as a problem would
 * bury the handful of rows that actually are one.
 */
export function CalendarSyncPage(): ReactNode {
  const token = useToken()
  const [filter, setFilter] = useState<'' | CalendarSyncStatus>('')

  const summary = useResource(() => api.calendarSummary(token), [token])
  const jobs = useResource(() => api.calendarJobs(token, filter || undefined), [token, filter])

  const retry = useAction(async (id: string) => {
    await api.retryCalendarJob(token, id)
    jobs.reload()
    summary.reload()
  })

  const nothingAtAll =
    summary.data !== null &&
    summary.data.pending + summary.data.synced + summary.data.skipped + summary.data.failed === 0

  return (
    <>
      <PageHeader
        title="Calendar sync"
        description="What each participant's Google Calendar should show, and whether Google agrees yet."
      />

      <div className="stats">
        <Stat label="Queued" value={summary.data?.pending} tone="info" />
        <Stat label="In sync" value={summary.data?.synced} tone="success" />
        <Stat label="No calendar" value={summary.data?.skipped} tone="neutral" />
        <Stat label="Failed" value={summary.data?.failed} tone="danger" emphasise />
      </div>

      {nothingAtAll && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert tone="info" title="Nothing to sync yet">
            Entries appear here once a patient or doctor connects a Google Calendar. If this
            deployment has no Google credentials configured, calendar sync stays switched off
            and everything else works normally.
          </Alert>
        </div>
      )}

      {retry.error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert title="Could not requeue">{retry.error}</Alert>
        </div>
      )}

      <div className="ops__filter">
        <label className="sr-only" htmlFor="calendar-status">
          Filter by status
        </label>
        <Select
          id="calendar-status"
          value={filter}
          onChange={(event) => setFilter(event.target.value as '' | CalendarSyncStatus)}
        >
          {FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </div>

      <DataState resource={jobs} empty={<EmptyState title="No calendar entries" />}>
        {(rows) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Wanted state</th>
                  <th>Status</th>
                  <th>Attempts</th>
                  <th>Last synced</th>
                  <th>Detail</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((job) => {
                  const status = calendarStatus(job.status)
                  return (
                    <tr key={job.id}>
                      <td>
                        {job.action === 'sync' ? 'Event present' : 'Event removed'}
                        <span className="ops__sub mono">{job.calendar_id}</span>
                      </td>
                      <td>
                        <Pill tone={status.tone}>{status.label}</Pill>
                      </td>
                      <td className="nowrap">{job.attempts}</td>
                      <td className="nowrap">{job.synced_at ? formatDateTime(job.synced_at) : '—'}</td>
                      <td className="ops__detail">
                        {job.last_error ??
                          (job.next_attempt_at
                            ? `Retrying ${formatDateTime(job.next_attempt_at)}`
                            : humanise(job.status))}
                      </td>
                      <td>
                        {job.status !== 'pending' && job.status !== 'synced' && (
                          <Button
                            variant="secondary"
                            loading={retry.pending}
                            onClick={() => retry.run(job.id)}
                          >
                            Requeue
                          </Button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </DataState>
    </>
  )
}
