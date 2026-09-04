import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from './gateway'
import {
  dispatchNativeNotification,
  NATIVE_NOTIFICATION_KINDS,
  respondToApprovalAction,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from './native-notifications'
import { $approvalRequest, getApprovalRequest, setApprovalRequest } from './prompts'
import { $activeSessionId, setActiveSessionId } from './session'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notify = vi.fn().mockResolvedValue(true)

function setWindowState({ focused = true, hidden = false }: { focused?: boolean; hidden?: boolean }) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => focused })
}

let counter = 0

// Unique session id per call dodges the per-(kind,session) throttle so each
// assertion starts clean.
function freshSession(): string {
  counter += 1

  return `session-${counter}`
}

beforeEach(() => {
  notify.mockClear()
  desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
  setNativeNotifyEnabled(true)

  for (const kind of NATIVE_NOTIFICATION_KINDS) {
    setNativeNotifyKind(kind, true)
  }

  setActiveSessionId(null)
  setWindowState({ focused: false, hidden: true })
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('dispatchNativeNotification focus gating', () => {
  it('fires a completion notification for the active session when the window is hidden', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('fires a completion notification when the window is visible but unfocused (alt-tab)', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setWindowState({ focused: false, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a completion notification when the window is focused', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setWindowState({ focused: true, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses a completion notification for a non-active background session (no gateway spam)', () => {
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'turnDone', sessionId: 'busy-bot-session', title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires an attention notification for an off-screen session even when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'background', title: 'approve' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses an attention notification for the active session when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'on-screen', title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires a global completion notification while away with no active session (pet gen)', () => {
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a global notification when the window is focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('dispatchNativeNotification preferences', () => {
  it('suppresses everything when the master switch is off', () => {
    setNativeNotifyEnabled(false)
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses only the disabled kind', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setNativeNotifyKind('turnDone', false)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()

    dispatchNativeNotification({ kind: 'turnError', sessionId, title: 'boom' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('forwards kind, sessionId, and requestId to the bridge', () => {
    setActiveSessionId('abc')
    dispatchNativeNotification({
      body: 'hi',
      kind: 'turnError',
      requestId: 'a'.repeat(32),
      sessionId: 'abc',
      title: 'boom'
    })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        body: 'hi',
        kind: 'turnError',
        requestId: 'a'.repeat(32),
        sessionId: 'abc',
        title: 'boom'
      })
    )
  })
})

describe('dispatchNativeNotification throttle', () => {
  it('collapses duplicate kind+session within the throttle window', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done again' })
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('sendTestNativeNotification', () => {
  it('fires regardless of focus or active session', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    sendTestNativeNotification('Hermes', 'works')
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('$activeSessionId wiring', () => {
  it('reflects the setter used for gating', () => {
    setActiveSessionId('xyz')
    expect($activeSessionId.get()).toBe('xyz')
  })
})

describe('respondToApprovalAction', () => {
  const request = vi.fn().mockResolvedValue({ resolved: 1 })

  beforeEach(() => {
    request.mockClear()
    setApprovalRequest(null)
    $gateway.set({ request } as unknown as ReturnType<typeof $gateway.get>)
  })

  afterEach(() => {
    $gateway.set(null)
  })

  it('approves via approval.respond {choice: "once"} and clears the prompt', async () => {
    setActiveSessionId('bg')
    setApprovalRequest({ command: 'rm -rf /', description: 'dangerous', requestId: 'a'.repeat(32), sessionId: 'bg' })

    await respondToApprovalAction('bg', 'approve', 'a'.repeat(32))

    expect(request).toHaveBeenCalledWith('approval.respond', {
      choice: 'once',
      request_id: 'a'.repeat(32),
      session_id: 'bg'
    })
    expect($approvalRequest.get()).toBeNull()
  })

  it('rejects via approval.respond {choice: "deny"}', async () => {
    setApprovalRequest({ command: 'rm -rf /', description: 'dangerous', requestId: 'b'.repeat(32), sessionId: 'bg' })
    await respondToApprovalAction('bg', 'reject', 'b'.repeat(32))
    expect(request).toHaveBeenCalledWith('approval.respond', {
      choice: 'deny',
      request_id: 'b'.repeat(32),
      session_id: 'bg'
    })
  })

  it('resolves a correlated queued approval out of order without consuming the head', async () => {
    const firstId = 'a'.repeat(32)
    const secondId = 'b'.repeat(32)
    setApprovalRequest({ command: 'first', description: 'A', requestId: firstId, sessionId: 'bg' })
    setApprovalRequest({ command: 'second', description: 'B', requestId: secondId, sessionId: 'bg' })

    await respondToApprovalAction('bg', 'reject', secondId)

    expect(request).toHaveBeenCalledWith('approval.respond', {
      choice: 'deny',
      request_id: secondId,
      session_id: 'bg'
    })
    expect(getApprovalRequest('bg')?.requestId).toBe(firstId)
    expect(getApprovalRequest('bg', secondId)).toBeNull()
  })

  it('keeps the correlated approval parked when the backend resolves zero requests', async () => {
    const requestId = 'c'.repeat(32)
    request.mockResolvedValueOnce({ resolved: 0 })
    setApprovalRequest({ command: 'still pending', description: 'C', requestId, sessionId: 'bg' })

    await respondToApprovalAction('bg', 'approve', requestId)

    expect(getApprovalRequest('bg', requestId)?.requestId).toBe(requestId)
  })

  it('ignores a stale notification for A after B becomes the live approval', async () => {
    setActiveSessionId('bg')
    setApprovalRequest({ command: 'command B', description: 'new approval', requestId: 'b'.repeat(32), sessionId: 'bg' })

    await respondToApprovalAction('bg', 'approve', 'a'.repeat(32))

    expect(request).not.toHaveBeenCalled()
    expect($approvalRequest.get()?.requestId).toBe('b'.repeat(32))
  })

  it('ignores a stale native action when the correlated prompt no longer exists', async () => {
    await respondToApprovalAction('bg', 'approve', 'a'.repeat(32))
    expect(request).not.toHaveBeenCalled()
  })

  it('ignores unknown action ids', async () => {
    await respondToApprovalAction('bg', 'snooze', 'a'.repeat(32))
    expect(request).not.toHaveBeenCalled()
  })

  it('no-ops without a gateway', async () => {
    $gateway.set(null)
    await respondToApprovalAction('bg', 'approve', 'a'.repeat(32))
    expect(request).not.toHaveBeenCalled()
  })
})
