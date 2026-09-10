import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { hydrateFullConfig } from '../app/useConfigSync.js'
import { sectionMode } from '../domain/details.js'

const payload = (mode: string) => ({
  config: { display: { show_reasoning: mode !== 'hidden', sections: { thinking: mode, tools: 'collapsed' } } }
})
const thinkingMode = () => {
  const state = getUiState()
  return sectionMode('thinking', state.detailsMode, state.sections, state.detailsModeCommandOverride)
}

describe('session reasoning config hydration', () => {
  beforeEach(resetUiState)

  it('ignores an older request for the same session', async () => {
    let finish!: (value: ReturnType<typeof payload>) => void
    const gw = { request: vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValueOnce(payload('collapsed')) } as any
    patchUiState({ sid: 'a' })
    const bell = vi.fn()
    const old = hydrateFullConfig(gw, bell)
    await hydrateFullConfig(gw, bell)
    finish(payload('expanded'))
    await old
    expect(thinkingMode()).toBe('collapsed')
    expect(bell).toHaveBeenCalledTimes(1)
  })

  it('ignores a response when the session changed without a replacement request', async () => {
    let finish!: (value: ReturnType<typeof payload>) => void
    const gw = { request: vi.fn(() => new Promise(resolve => { finish = resolve })) } as any
    patchUiState({ sid: 'a', sections: { thinking: 'hidden' }, showReasoning: false })
    const bell = vi.fn()
    const old = hydrateFullConfig(gw, bell)
    patchUiState({ sid: 'b' })
    finish(payload('expanded'))
    await old
    expect(thinkingMode()).toBe('hidden')
    expect(bell).not.toHaveBeenCalled()
  })

  it('uses the selected session on initial hydration, reload and session switch', async () => {
    const gw = { request: vi.fn((_method: string, params: { session_id?: string }) =>
      Promise.resolve(payload(params.session_id === 'a' ? 'expanded' : 'collapsed'))) } as any
    const bell = vi.fn()
    for (const sid of ['a', 'b', 'a']) {
      patchUiState({ sid })
      await hydrateFullConfig(gw, bell)
      expect(gw.request).toHaveBeenLastCalledWith('config.get', { key: 'full', session_id: sid })
      expect(thinkingMode()).toBe(sid === 'a' ? 'expanded' : 'collapsed')
      expect(getUiState().showReasoning).toBe(true)
      // Same path as the mtime poller; unrelated global reload must keep this override.
      await hydrateFullConfig(gw, bell)
      expect(thinkingMode()).toBe(sid === 'a' ? 'expanded' : 'collapsed')
    }
  })

  it('does not apply an old session response after switching chats', async () => {
    let finishA!: (value: ReturnType<typeof payload>) => void
    const gw = { request: vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finishA = resolve }))
      .mockResolvedValueOnce(payload('hidden')) } as any
    patchUiState({ sid: 'a' })
    const old = hydrateFullConfig(gw, vi.fn())
    patchUiState({ sid: 'b' })
    await hydrateFullConfig(gw, vi.fn())
    expect(thinkingMode()).toBe('hidden')
    finishA(payload('expanded'))
    await old
    expect(thinkingMode()).toBe('hidden')
    expect(getUiState().showReasoning).toBe(false)
  })
})
