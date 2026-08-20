/** The action contract.
 *
 * These exist because of a bug found in a browser, not in a test: `run` used to return
 * `Result | undefined`, so an action whose job was purely a side effect returned `undefined`
 * on success — indistinguishable from failure. The symptom form guarded on it, and a booking
 * that the API had already accepted with a 200 silently failed to navigate. The shape below
 * makes that mistake unrepresentable; these tests keep it that way.
 */

import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import { useAction, useResource } from './useResource'

describe('useAction', () => {
  it('reports success even when the action returns nothing', async () => {
    const { result } = renderHook(() => useAction(async () => {}))

    let outcome!: Awaited<ReturnType<typeof result.current.run>>
    await act(async () => {
      outcome = await result.current.run()
    })

    expect(outcome.ok).toBe(true)
  })

  it('carries the value through on success', async () => {
    const { result } = renderHook(() => useAction(async (n: number) => n * 2))

    let outcome!: Awaited<ReturnType<typeof result.current.run>>
    await act(async () => {
      outcome = await result.current.run(21)
    })

    expect(outcome).toEqual({ ok: true, value: 42 })
  })

  it('reports failure without rethrowing', async () => {
    const { result } = renderHook(() =>
      useAction(async () => {
        throw new ApiError(409, 'That slot was just taken.')
      }),
    )

    let outcome!: Awaited<ReturnType<typeof result.current.run>>
    await act(async () => {
      // An unhandled rejection inside an onClick is how this would otherwise be missed.
      outcome = await result.current.run()
    })

    expect(outcome.ok).toBe(false)
    expect(result.current.error).toBe('That slot was just taken.')
  })

  it('exposes field errors from a validation failure', async () => {
    const { result } = renderHook(() =>
      useAction(async () => {
        throw new ApiError(422, 'bad', { password: 'too short' })
      }),
    )

    await act(async () => {
      await result.current.run()
    })

    expect(result.current.fieldErrors).toEqual({ password: 'too short' })
  })

  it('clears the previous error when run again', async () => {
    let shouldFail = true
    const { result } = renderHook(() =>
      useAction(async () => {
        if (shouldFail) throw new ApiError(500, 'boom')
      }),
    )

    await act(async () => {
      await result.current.run()
    })
    expect(result.current.error).toBe('boom')

    shouldFail = false
    await act(async () => {
      await result.current.run()
    })
    expect(result.current.error).toBeNull()
  })
})

describe('useResource', () => {
  it('loads and exposes data', async () => {
    const { result } = renderHook(() => useResource(async () => 'hello', []))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).toBe('hello')
    expect(result.current.error).toBeNull()
  })

  it('turns a failure into a message rather than throwing', async () => {
    const { result } = renderHook(() =>
      useResource(async () => {
        throw new ApiError(503, 'Calendar is not configured.')
      }, []),
    )

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe('Calendar is not configured.')
  })

  it('refetches on reload', async () => {
    const load = vi.fn(async () => 'value')
    const { result } = renderHook(() => useResource(load, []))

    await waitFor(() => expect(result.current.loading).toBe(false))
    const before = load.mock.calls.length

    act(() => result.current.reload())
    await waitFor(() => expect(load.mock.calls.length).toBe(before + 1))
  })

  it('does not report an aborted request as an error', async () => {
    // A screen the user has left is not a failure to show them.
    const { result, unmount } = renderHook(() =>
      useResource(
        (signal) =>
          new Promise<string>((_resolve, reject) => {
            signal.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            )
          }),
        [],
      ),
    )

    unmount()
    expect(result.current.error).toBeNull()
  })
})
