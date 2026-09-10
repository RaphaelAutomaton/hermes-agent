import type { ApprovalRespondResponse } from '../gatewayTypes.js'
import type { ApprovalReq } from '../types.js'

import type { GatewayRpc } from './interfaces.js'
import { removeApproval } from './overlayStore.js'

export type ApprovalChoice = 'always' | 'deny' | 'once' | 'session'

/** Resolve one observed approval and mutate local state only after exact backend confirmation. */
export async function respondToApproval(
  rpc: GatewayRpc,
  approval: ApprovalReq,
  choice: ApprovalChoice
): Promise<boolean> {
  const response = await rpc<ApprovalRespondResponse>('approval.respond', {
    choice,
    request_id: approval.requestId,
    session_id: approval.sessionId
  })

  if (response?.resolved !== 1) {
    return false
  }

  return removeApproval(approval.sessionId, approval.requestId)
}
