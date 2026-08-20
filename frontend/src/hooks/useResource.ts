/** Loading data, and running actions that change it.
 *
 * Two small hooks instead of a query library. What this app actually needs from one is:
 * fetch on mount, show a spinner, show an error, refetch after a mutation, and do not write
 * state from a request whose screen has gone. That is the code below. Caching, background
 * refetching and query invalidation would be genuinely useful in a larger app — here they
 * would be a dependency earning its keep on one screen out of fifteen.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../api/client'

export function messageFor(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

export interface Resource<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** Refetch. Call after a mutation that changes what this resource returns. */
  reload: () => void
}

/**
 * @param load  Receives an `AbortSignal`; pass it to the request so a screen the user has
 *              left stops fetching.
 * @param deps  Refetch when these change — the same contract as `useEffect`.
 */
export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): Resource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  // Held in a ref so changing the closure (which happens on every render) does not itself
  // trigger a refetch. `deps` decides when to reload; the function is just how.
  const loadRef = useRef(load)

  // Written in an effect rather than during render. Assigning to a ref while rendering is the
  // familiar idiom, but a render can be thrown away and re-run under concurrent React, which
  // would leave the ref pointing at a closure from an abandoned attempt. This effect is
  // declared first, so it has run before the fetch effect below on every commit.
  useEffect(() => {
    loadRef.current = load
  })

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false

    setLoading(true)
    setError(null)

    loadRef
      .current(controller.signal)
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) return
        setError(messageFor(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  return { data, error, loading, reload }
}

/** Whether the action ran, kept separate from whatever it returned.
 *
 * Deliberately not `Result | undefined`. That shape cannot tell "it failed" apart from "it
 * succeeded and returned nothing", so an action whose whole job is a side effect looks
 * identical to a failure — and a caller guarding on it silently stops navigating after a
 * booking that in fact went through. Found exactly that way, in a browser, after the API had
 * already answered 200.
 */
export type ActionResult<T> = { ok: true; value: T } | { ok: false }

export interface Action<Args extends unknown[], Result> {
  run: (...args: Args) => Promise<ActionResult<Result>>
  pending: boolean
  error: string | null
  /** Field-level messages from a 422, so a form can show them beside the right input. */
  fieldErrors: Record<string, string>
  reset: () => void
}

/**
 * Wraps a mutation so a button gets `pending` and `error` without every screen repeating the
 * same try/catch. Reports failure in the return value rather than rethrowing: the error is
 * already in state, and an unhandled rejection in an `onClick` is the usual way that gets
 * missed.
 */
export function useAction<Args extends unknown[], Result>(
  perform: (...args: Args) => Promise<Result>,
): Action<Args, Result> {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  const performRef = useRef(perform)

  useEffect(() => {
    performRef.current = perform
  })

  const reset = useCallback(() => {
    setError(null)
    setFieldErrors({})
  }, [])

  const run = useCallback(async (...args: Args): Promise<ActionResult<Result>> => {
    setPending(true)
    setError(null)
    setFieldErrors({})
    try {
      return { ok: true, value: await performRef.current(...args) }
    } catch (cause) {
      setError(messageFor(cause))
      if (cause instanceof ApiError) setFieldErrors(cause.fieldErrors)
      return { ok: false }
    } finally {
      setPending(false)
    }
  }, [])

  return { run, pending, error, fieldErrors, reset }
}
