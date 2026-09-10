import { beforeEach, describe, expect, it, vi } from 'vitest'

import { respondToApproval } from '../app/approvalResponses.js'
import type { GatewayRpc } from '../app/interfaces.js'
import { enqueueApproval, getOverlayState, removeApproval, resetOverlayState } from '../app/overlayStore.js'
import type { ApprovalReq } from '../types.js'

const approval = (requestId: string, sessionId = 'sess-1'): ApprovalReq => ({
  command: requestId,
  description: requestId,
  requestId,
  sessionId
})

const rpcReturning = (value: unknown) => vi.fn(async () => value) as unknown as GatewayRpc

describe('respondToApproval', () => {
  beforeEach(() => resetOverlayState())

  it('sends the captured session and removes an exact queued request out of order', async () => {
    const first = approval('a'.repeat(32))
    const second = approval('b'.repeat(32))
    const rpc = rpcReturning({ resolved: 1 })
    enqueueApproval(first)
    enqueueApproval(second)

    await expect(respondToApproval(rpc, second, 'deny')).resolves.toBe(true)
    expect(rpc).toHaveBeenCalledWith('approval.respond', {
      choice: 'deny',
      request_id: second.requestId,
      session_id: second.sessionId
    })
    expect(getOverlayState().approval).toEqual(first)
    expect(getOverlayState().approvalQueue).toEqual([])
  })

  it('preserves the request when the backend resolves zero approvals', async () => {
    const request = approval('a'.repeat(32))
    enqueueApproval(request)

    await expect(respondToApproval(rpcReturning({ resolved: 0 }), request, 'once')).resolves.toBe(false)
    expect(getOverlayState().approval).toEqual(request)
  })

  it('preserves the request when the RPC fails', async () => {
    const request = approval('a'.repeat(32))
    const rpc = vi.fn(async () => {
      throw new Error('offline')
    }) as unknown as GatewayRpc
    enqueueApproval(request)

    await expect(respondToApproval(rpc, request, 'once')).rejects.toThrow('offline')
    expect(getOverlayState().approval).toEqual(request)
  })

  it('does not consume a newer request when a stale response arrives', async () => {
    const stale = approval('a'.repeat(32))
    const current = approval('b'.repeat(32))
    enqueueApproval(stale)
    expect(removeApproval(stale.sessionId, stale.requestId)).toBe(true)
    enqueueApproval(current)

    await expect(respondToApproval(rpcReturning({ resolved: 1 }), stale, 'deny')).resolves.toBe(false)
    expect(getOverlayState().approval).toEqual(current)
  })
})
