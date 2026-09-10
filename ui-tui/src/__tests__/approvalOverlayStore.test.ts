import { beforeEach, describe, expect, it } from 'vitest'

import { enqueueApproval, getOverlayState, removeApproval, resetOverlayState } from '../app/overlayStore.js'
import type { ApprovalReq } from '../types.js'

const approval = (requestId: string, sessionId = 'sess-1'): ApprovalReq => ({
  command: requestId,
  description: requestId,
  requestId,
  sessionId
})

describe('approval overlay ledger', () => {
  beforeEach(() => resetOverlayState())

  it('deduplicates replays and preserves FIFO presentation', () => {
    const first = approval('a'.repeat(32))
    const second = approval('b'.repeat(32))

    expect(enqueueApproval(first)).toBe(true)
    expect(enqueueApproval(second)).toBe(true)
    expect(enqueueApproval(second)).toBe(false)
    expect(getOverlayState().approval).toEqual(first)
    expect(getOverlayState().approvalQueue).toEqual([second])
  })

  it('removes only the exact queued request without consuming the head', () => {
    const first = approval('a'.repeat(32))
    const second = approval('b'.repeat(32))
    enqueueApproval(first)
    enqueueApproval(second)

    expect(removeApproval('sess-1', second.requestId)).toBe(true)
    expect(getOverlayState().approval).toEqual(first)
    expect(getOverlayState().approvalQueue).toEqual([])
  })

  it('promotes the next request only after the exact head is removed', () => {
    const first = approval('a'.repeat(32))
    const second = approval('b'.repeat(32))
    enqueueApproval(first)
    enqueueApproval(second)

    expect(removeApproval('wrong-session', first.requestId)).toBe(false)
    expect(removeApproval('sess-1', 'c'.repeat(32))).toBe(false)
    expect(getOverlayState().approval).toEqual(first)

    expect(removeApproval('sess-1', first.requestId)).toBe(true)
    expect(getOverlayState().approval).toEqual(second)
    expect(getOverlayState().approvalQueue).toEqual([])
  })
})
