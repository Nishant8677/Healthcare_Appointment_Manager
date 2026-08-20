import type { ReactNode } from 'react'

import { EmptyState, LinkButton } from '../components/ui'

export function NotFoundPage(): ReactNode {
  return (
    <div style={{ padding: '4rem 1.25rem' }}>
      <EmptyState
        title="That page does not exist"
        action={<LinkButton to="/" variant="primary">Take me back</LinkButton>}
      >
        The link may be out of date, or the address mistyped.
      </EmptyState>
    </div>
  )
}
