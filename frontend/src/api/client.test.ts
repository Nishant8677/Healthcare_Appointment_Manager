/** How the app reads an API failure.
 *
 * Worth testing directly because every error the user ever sees passes through `describe()`,
 * and FastAPI answers with two different shapes: a plain string for a business rule, and an
 * array of `{loc, msg}` for validation. Getting the second wrong renders "[object Object]"
 * beside a form field.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, request, setUnauthorizedHandler } from './client'

function respondWith(status: number, body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Promise.resolve(
        new Response(body === undefined ? '' : JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    ),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('request', () => {
  it('returns the parsed body on success', async () => {
    respondWith(200, { id: 'abc' })
    await expect(request<{ id: string }>('/thing')).resolves.toEqual({ id: 'abc' })
  })

  it('handles a 204 with no body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.resolve(new Response(null, { status: 204 }))))
    await expect(request('/thing', { method: 'DELETE' })).resolves.toBeUndefined()
  })

  it('surfaces the API message for a business-rule failure', async () => {
    // The backend writes these for a person to read — "That slot was just taken by another
    // patient." — so they must reach the screen verbatim rather than being replaced.
    respondWith(409, { detail: 'That slot was just taken by another patient.' })
    await expect(request('/appointments/hold', { method: 'POST' })).rejects.toThrow(
      'That slot was just taken by another patient.',
    )
  })

  it('splits a validation error into per-field messages', async () => {
    respondWith(422, {
      detail: [
        { loc: ['body', 'password'], msg: 'String should have at least 10 characters' },
        { loc: ['body', 'email'], msg: 'value is not a valid email address' },
      ],
    })

    const error = await request('/auth/register', { method: 'POST' }).catch((cause) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).fieldErrors).toEqual({
      password: 'String should have at least 10 characters',
      email: 'value is not a valid email address',
    })
  })

  it('keeps the first message per field rather than the last', async () => {
    respondWith(422, {
      detail: [
        { loc: ['body', 'password'], msg: 'too short' },
        { loc: ['body', 'password'], msg: 'also wrong' },
      ],
    })
    const error = (await request('/x', { method: 'POST' }).catch((cause) => cause)) as ApiError
    expect(error.fieldErrors.password).toBe('too short')
  })

  it('signals an expired session exactly once, so the app can sign out', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    respondWith(401, { detail: 'Not authenticated.' })

    await expect(request('/auth/me')).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not treat other failures as a lost session', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    respondWith(403, { detail: 'Forbidden.' })

    await expect(request('/admin/doctors')).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('explains a network failure in words a patient can act on', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))))
    await expect(request('/thing')).rejects.toThrow(/Could not reach the server/)
  })

  it('does not swallow an aborted request', async () => {
    // Aborting is a screen being left, not a failure — reporting it would flash an error at
    // someone who has already navigated away.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new DOMException('aborted', 'AbortError'))),
    )
    await expect(request('/thing')).rejects.toBeInstanceOf(DOMException)
  })

  it('falls back to a readable message when the body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.resolve(new Response('<html>502</html>', { status: 502 }))),
    )
    await expect(request('/thing')).rejects.toThrow(/server had a problem/)
  })

  it('sends the bearer token when given one', async () => {
    const fetchMock = vi.fn(async (_url: URL | string, _init?: RequestInit) =>
      Promise.resolve(new Response('{}', { status: 200 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    await request('/auth/me', { token: 'a-token' })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer a-token')
  })

  it('omits undefined query parameters instead of sending "undefined"', async () => {
    const fetchMock = vi.fn(async (_url: URL | string, _init?: RequestInit) =>
      Promise.resolve(new Response('[]', { status: 200 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    await request('/doctors', { query: { specialisation: undefined, include_inactive: false } })

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).not.toContain('specialisation')
    expect(url).toContain('include_inactive=false')
  })

  it('marks conflicts so a screen can offer to refresh', async () => {
    respondWith(410, { detail: 'The hold on this slot has expired.' })
    const error = (await request('/x', { method: 'POST' }).catch((cause) => cause)) as ApiError
    expect(error.isConflict).toBe(true)
  })
})
