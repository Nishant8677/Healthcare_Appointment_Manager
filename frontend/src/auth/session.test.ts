import { beforeEach, describe, expect, it } from 'vitest'

import { clearSession, isUsable, loadSession, saveSession } from './session'

describe('session storage', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('round-trips a token', () => {
    saveSession('a-token', 3600)
    expect(loadSession()?.token).toBe('a-token')
  })

  it('discards a token that is about to expire', () => {
    // A token with 30 seconds left would be sent, then rejected mid-request. The margin
    // makes the app treat it as already gone and ask for a fresh sign-in instead.
    saveSession('nearly-dead', 30)
    expect(loadSession()).toBeNull()
  })

  it('keeps a token with comfortable life left', () => {
    saveSession('healthy', 3600)
    expect(loadSession()).not.toBeNull()
  })

  it('treats a corrupt entry as signed out rather than crashing on boot', () => {
    // Hand-edited, half-written, or left over from an older version of the app. Throwing
    // here would take down the whole application before it rendered anything.
    window.sessionStorage.setItem('ham.session', '{not json')
    expect(loadSession()).toBeNull()
  })

  it('rejects a well-formed entry with the wrong shape', () => {
    window.sessionStorage.setItem('ham.session', JSON.stringify({ token: 42 }))
    expect(loadSession()).toBeNull()
  })

  it('removes the entry once it is found to be unusable', () => {
    saveSession('nearly-dead', 30)
    loadSession()
    expect(window.sessionStorage.getItem('ham.session')).toBeNull()
  })

  it('clears on request', () => {
    saveSession('a-token', 3600)
    clearSession()
    expect(loadSession()).toBeNull()
  })

  it('uses sessionStorage, so closing the tab ends the session', () => {
    // The deliberate choice: a shared clinic workstation must not keep a doctor signed in
    // for whoever sits down next.
    saveSession('a-token', 3600)
    expect(window.sessionStorage.getItem('ham.session')).not.toBeNull()
    expect(window.localStorage.getItem('ham.session')).toBeNull()
  })

  it('judges usability against an injected clock', () => {
    const session = { token: 't', expiresAt: 1_000_000 }
    expect(isUsable(session, 0)).toBe(true)
    expect(isUsable(session, 999_000)).toBe(false)
    expect(isUsable(null, 0)).toBe(false)
  })
})
