import { useEffect, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

import { ApiError } from '../api/client'
import { calendar as api } from '../api/endpoints'
import { useToken } from '../auth/useAuth'
import { Alert, Button, Card, CardHeader, PageHeader, Pill, Spinner } from '../components/ui'
import { useAction, useResource } from '../hooks/useResource'
import { formatDateTime } from '../lib/format'

export function CalendarSettingsPage(): ReactNode {
  const token = useToken()
  const [params, setParams] = useSearchParams()
  const [notConfigured, setNotConfigured] = useState(false)

  const resource = useResource(() => api.connection(token), [token])
  const connection = resource.data

  // The OAuth callback bounces the browser back here with ?calendar=connected|declined when
  // CALENDAR_RETURN_URL points at this page.
  const outcome = params.get('calendar')
  useEffect(() => {
    if (outcome === null) return
    resource.reload()
    // Cleared so a refresh does not keep re-announcing a connection made minutes ago.
    const next = new URLSearchParams(params)
    next.delete('calendar')
    setParams(next, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outcome])

  const connect = useAction(async () => {
    try {
      const { authorization_url } = await api.connect(token)
      // A full navigation, not a popup: Google refuses to render its consent screen inside a
      // frame, and popup blockers eat the alternative.
      window.location.assign(authorization_url)
    } catch (error) {
      // 503 means this deployment has no Google credentials — a supported configuration, not
      // a fault, so it gets an explanation rather than an error banner.
      if (error instanceof ApiError && error.status === 503) {
        setNotConfigured(true)
        return
      }
      throw error
    }
  })

  const disconnect = useAction(async () => {
    await api.disconnect(token)
    resource.reload()
  })

  return (
    <>
      <PageHeader
        title="Google Calendar"
        description="Connect your calendar and your appointments appear in it automatically — added when you book, moved when you reschedule, removed when you cancel."
      />

      {outcome === 'declined' && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert tone="warning" title="Access was not granted">
            You cancelled at the Google consent screen, so nothing was connected.
          </Alert>
        </div>
      )}

      <Card>
        {resource.loading && connection === null ? (
          <Spinner label="Checking your calendar" />
        ) : resource.error !== null ? (
          <Alert title="Could not check your calendar">{resource.error}</Alert>
        ) : connection?.connected ? (
          <>
            <CardHeader
              title="Connected"
              subtitle={connection.google_account_email ?? undefined}
              actions={<Pill tone="success">Active</Pill>}
            />
            {connection.connected_at && (
              <p className="summary__muted">
                Connected on {formatDateTime(connection.connected_at)}.
              </p>
            )}
            <div className="symptom__footer">
              <Button variant="danger" loading={disconnect.pending} onClick={() => disconnect.run()}>
                Disconnect
              </Button>
            </div>
            <p className="summary__meta">
              Disconnecting withdraws access at Google and deletes the stored credential.
              Events already in your calendar are left alone.
            </p>
          </>
        ) : (
          <>
            <CardHeader
              title="Not connected"
              subtitle="Your appointments are not being written to a calendar."
              actions={
                connection?.revoked_at ? <Pill tone="warning">Needs reconnecting</Pill> : undefined
              }
            />

            {connection?.revoked_at && (
              <div style={{ marginBottom: '1rem' }}>
                <Alert tone="warning" title="Your previous connection stopped working">
                  {connection.last_error ??
                    'Access was withdrawn at Google. Reconnect to start syncing again.'}
                </Alert>
              </div>
            )}

            {notConfigured ? (
              <Alert tone="info" title="Calendar sync is not set up on this deployment">
                <p>
                  Everything else works normally — appointments, emails and summaries are
                  unaffected. Calendar events are simply not created.
                </p>
              </Alert>
            ) : (
              <>
                {connect.error && (
                  <div style={{ marginBottom: '1rem' }}>
                    <Alert title="Could not start">{connect.error}</Alert>
                  </div>
                )}
                <Button variant="primary" loading={connect.pending} onClick={() => connect.run()}>
                  Connect Google Calendar
                </Button>
                <p className="summary__meta">
                  You will be sent to Google to approve access. The clinic can add, change and
                  remove its own appointments in your calendar — nothing else.
                </p>
              </>
            )}
          </>
        )}
      </Card>
    </>
  )
}
