import { atom, computed } from 'nanostores'

import type { ApprovalReq } from '../types.js'

import type { OverlayState } from './interfaces.js'

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsInitialHistoryIndex: 0,
  approval: null,
  approvalQueue: [],
  billing: null,
  clarify: null,
  confirm: null,
  journey: false,
  modelPicker: false,
  pager: null,
  petPicker: false,
  pluginsHub: false,
  secret: null,
  sessions: false,
  skillsHub: false,
  sudo: null
})

export const $overlayState = atom<OverlayState>(buildOverlayState())

export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    approval,
    billing,
    clarify,
    confirm,
    journey,
    modelPicker,
    pager,
    petPicker,
    pluginsHub,
    secret,
    sessions,
    skillsHub,
    sudo
  }) =>
    Boolean(
      agents ||
      approval ||
      billing ||
      clarify ||
      confirm ||
      journey ||
      modelPicker ||
      pager ||
      petPicker ||
      pluginsHub ||
      secret ||
      sessions ||
      skillsHub ||
      sudo
    )
)

export const getOverlayState = () => $overlayState.get()

export const patchOverlayState = (next: Partial<OverlayState> | ((state: OverlayState) => OverlayState)) =>
  $overlayState.set(typeof next === 'function' ? next($overlayState.get()) : { ...$overlayState.get(), ...next })

/** Park one approval without overwriting an earlier request or duplicating a replay. */
export const enqueueApproval = (request: ApprovalReq): boolean => {
  const state = $overlayState.get()
  const exists = [state.approval, ...state.approvalQueue].some(
    current => current?.sessionId === request.sessionId && current.requestId === request.requestId
  )

  if (exists) {
    return false
  }

  if (!state.approval) {
    $overlayState.set({ ...state, approval: request })
  } else {
    $overlayState.set({ ...state, approvalQueue: [...state.approvalQueue, request] })
  }

  return true
}

/** Remove only the correlated approval and promote the next request when needed. */
export const removeApproval = (sessionId: string, requestId: string): boolean => {
  const state = $overlayState.get()

  if (state.approval?.sessionId === sessionId && state.approval.requestId === requestId) {
    const [approval = null, ...approvalQueue] = state.approvalQueue
    $overlayState.set({ ...state, approval, approvalQueue })

    return true
  }

  const approvalQueue = state.approvalQueue.filter(
    request => request.sessionId !== sessionId || request.requestId !== requestId
  )

  if (approvalQueue.length === state.approvalQueue.length) {
    return false
  }

  $overlayState.set({ ...state, approvalQueue })

  return true
}

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => $overlayState.set(buildOverlayState())

/**
 * Soft reset: drop FLOW-scoped overlays (approval / clarify / confirm / sudo
 * / secret / pager) but PRESERVE user-toggled ones — agents dashboard, model
 * picker, skills hub, sessions overlay.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 */
export const resetFlowOverlays = () =>
  $overlayState.set({
    ...buildOverlayState(),
    agents: $overlayState.get().agents,
    agentsInitialHistoryIndex: $overlayState.get().agentsInitialHistoryIndex,
    journey: $overlayState.get().journey,
    modelPicker: $overlayState.get().modelPicker,
    petPicker: $overlayState.get().petPicker,
    pluginsHub: $overlayState.get().pluginsHub,
    sessions: $overlayState.get().sessions,
    skillsHub: $overlayState.get().skillsHub
  })
