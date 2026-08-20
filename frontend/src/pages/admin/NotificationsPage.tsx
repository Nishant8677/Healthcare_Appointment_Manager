import { useState, type ReactNode } from 'react'

import { admin as api } from '../../api/endpoints'
import type { NotificationStatus } from '../../api/types'
import { useToken } from '../../auth/useAuth'
import { Alert, Button, DataState, EmptyState, PageHeader, Pill, Select } from '../../components/ui'
import { useAction, useResource } from '../../hooks/useResource'
import { formatDateTime, humanise, notificationStatus } from '../../lib/format'
import './Operations.css'

const FILTERS: Array<{ value: '' | NotificationStatus; label: string }> = [
  { value: '', label: 'All messages' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Queued' },
  { value: 'sent', label: 'Sent' },
]

/** The outbox, as an operator sees it.
 *
 * This screen exists because a message that failed and a message that was never raised look
 * identical from outside the system. Failed rows are parked rather than deleted precisely so
 * they can be seen here and put back in the queue once the underlying problem is fixed.
 */
export function NotificationsPage(): ReactNode {
  const token = useToken()
  const [filter, setFilter] = useState<'' | NotificationStatus>('')

  const summary = useResource(() => api.notificationSummary(token), [token])
  const jobs = useResource(
    () => api.notifications(token, filter || undefined),
    [token, filter],
  )

  const retry = useAction(async (id: string) => {
    await api.retryNotification(token, id)
    jobs.reload()
    summary.reload()
  })

  return (
    <>
      <PageHeader
        title="Notifications"
        description="Every email the clinic has queued. Delivery is retried automatically; anything here marked failed has exhausted its retries."
      />

      <div className="stats">
        <Stat label="Queued" value={summary.data?.pending} tone="info" />
        <Stat label="Sent" value={summary.data?.sent} tone="success" />
        <Stat label="Failed" value={summary.data?.failed} tone="danger" emphasise />
      </div>

      {retry.error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert title="Could not requeue">{retry.error}</Alert>
        </div>
      )}

      <div className="ops__filter">
        <label className="sr-only" htmlFor="status">
          Filter by status
        </label>
        <Select
          id="status"
          value={filter}
          onChange={(event) => setFilter(event.target.value as '' | NotificationStatus)}
        >
          {FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </div>

      <DataState resource={jobs} empty={<EmptyState title="Nothing queued" />}>
        {(rows) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Recipient</th>
                  <th>Due</th>
                  <th>Status</th>
                  <th>Attempts</th>
                  <th>Detail</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((job) => {
                  const status = notificationStatus(job.status)
                  return (
                    <tr key={job.id}>
                      <td>{humanise(job.notification_type)}</td>
                      <td className="mono">{job.recipient_email}</td>
                      <td className="nowrap">{formatDateTime(job.scheduled_for)}</td>
                      <td>
                        <Pill tone={status.tone}>{status.label}</Pill>
                      </td>
                      <td className="nowrap">{job.attempts}</td>
                      <td className="ops__detail">
                        {job.last_error ?? (job.sent_at ? formatDateTime(job.sent_at) : '—')}
                      </td>
                      <td>
                        {job.status === 'failed' && (
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

export function Stat({
  label,
  value,
  tone,
  emphasise = false,
}: {
  label: string
  value: number | undefined
  tone: 'info' | 'success' | 'danger' | 'neutral'
  emphasise?: boolean
}): ReactNode {
  return (
    <div className={`stat stat--${tone} ${emphasise && value ? 'is-loud' : ''}`}>
      <span className="stat__value">{value ?? '—'}</span>
      <span className="stat__label">{label}</span>
    </div>
  )
}
