/** The current time, as React state that advances on an interval.
 *
 * The clock is an external system, which is why reading `Date.now()` during render is a bug
 * rather than a style preference: two renders in the same commit can disagree, and nothing
 * re-renders when the time changes, so a view of "what is upcoming" silently goes stale on a
 * tab left open. Subscribing to it makes both problems go away.
 */

import { useEffect, useState } from 'react'

/**
 * @param intervalMs How often to advance. Use a second for a visible countdown, a minute for
 *                   anything coarser — an interval finer than what is displayed just wakes
 *                   the CPU to re-render identical output.
 */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])

  return now
}

export const ONE_SECOND = 1_000
export const ONE_MINUTE = 60_000
