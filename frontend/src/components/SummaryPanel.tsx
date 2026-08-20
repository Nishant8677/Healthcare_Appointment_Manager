/** An AI summary, in whichever of its three states it is actually in.
 *
 * The states matter more than the text. A summary that is not ready is reported as `pending`
 * or `failed` with a reason rather than as nothing, because "still being written" and "will
 * never arrive" call for different decisions from the person reading — wait, or carry on
 * without it. Collapsing them into a blank panel throws that distinction away.
 *
 * The line about the clinical record is not reassurance for its own sake: the notes and the
 * prescription *are* the record, and this text is commentary on it. A failed summary means
 * nothing medical is missing.
 */

import type { ReactNode } from 'react'

import type { AiSummary, PostVisitContent, PreVisitContent } from '../api/types'
import { urgency as urgencyDescriptor } from '../lib/format'
import { Alert, Card, CardHeader, Pill, Spinner } from './ui'
import './SummaryPanel.css'

interface Props {
  title: string
  subtitle?: string
  summary: AiSummary | null
  loading: boolean
  error: string | null
}

export function PreVisitSummaryPanel({
  summary,
  loading,
  error,
}: Omit<Props, 'title' | 'subtitle'>): ReactNode {
  const content = summary?.content as PreVisitContent | null | undefined

  return (
    <Card>
      <CardHeader
        title="Pre-visit brief"
        subtitle="Generated from what the patient described. Read it, do not rely on it."
        actions={
          summary?.status === 'ready' && summary.urgency ? (
            <Pill tone={urgencyDescriptor(summary.urgency).tone}>
              {urgencyDescriptor(summary.urgency).label}
            </Pill>
          ) : undefined
        }
      />

      <SummaryBody summary={summary} loading={loading} error={error}>
        {content && (
          <>
            <dl className="summary">
              <dt>Chief complaint</dt>
              <dd>{content.chief_complaint}</dd>
            </dl>
            {content.suggested_questions.length > 0 && (
              <>
                <h3 className="summary__heading">Questions worth asking</h3>
                <ul className="summary__list">
                  {content.suggested_questions.map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </SummaryBody>
    </Card>
  )
}

export function PostVisitSummaryPanel({
  summary,
  loading,
  error,
}: Omit<Props, 'title' | 'subtitle'>): ReactNode {
  const content = summary?.content as PostVisitContent | null | undefined

  return (
    <Card>
      <CardHeader
        title="Your visit summary"
        subtitle="Your doctor's notes, written in everyday language."
      />

      <SummaryBody summary={summary} loading={loading} error={error}>
        {content && (
          <>
            <p className="summary__prose">{content.summary}</p>

            {content.medication_schedule.length > 0 && (
              <>
                <h3 className="summary__heading">Your medicines</h3>
                <ul className="summary__list">
                  {content.medication_schedule.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </>
            )}

            {content.follow_up_steps.length > 0 && (
              <>
                <h3 className="summary__heading">What to do next</h3>
                <ul className="summary__list">
                  {content.follow_up_steps.map((step) => (
                    <li key={step}>{step}</li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </SummaryBody>
    </Card>
  )
}

function SummaryBody({
  summary,
  loading,
  error,
  children,
}: {
  summary: AiSummary | null
  loading: boolean
  error: string | null
  children: ReactNode
}): ReactNode {
  if (loading && summary === null) return <Spinner label="Loading the summary" />

  // A 404 here means the visit has not happened yet, which is not an error worth alarming
  // anyone about.
  if (error !== null) return <p className="summary__muted">{error}</p>

  if (summary === null) return null

  if (summary.status === 'pending') {
    return (
      <Alert tone="info" title="Still being prepared">
        <p>{summary.unavailable_reason ?? 'This usually takes a few seconds. Check back shortly.'}</p>
      </Alert>
    )
  }

  if (summary.status === 'failed') {
    return (
      <Alert tone="warning" title="This summary could not be generated">
        <p>{summary.unavailable_reason ?? 'The summary service did not answer.'}</p>
        <p>
          The clinical record is unaffected — the notes and prescription from the visit are
          complete.
        </p>
      </Alert>
    )
  }

  return <>{children}</>
}
