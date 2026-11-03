import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope, type Ref } from 'vue'
import EmbedTestApp from './EmbedTestApp.vue'

const boundary = vi.hoisted(() => ({
  get: vi.fn(),
  authorize: vi.fn(),
  mounted: [] as Array<() => Promise<void>>,
  unmounting: [] as Array<() => void>,
}))
vi.mock('axios', () => ({ default: { create: () => ({
  get: boundary.get,
  interceptors: { request: { use: boundary.authorize } },
}) } }))
vi.mock('vue', async importOriginal => ({
  ...await importOriginal<typeof import('vue')>(),
  useSSRContext: () => ({ modules: new Set<string>() }),
  onMounted: (callback: () => Promise<void>) => boundary.mounted.push(callback),
  onBeforeUnmount: (callback: () => void) => boundary.unmounting.push(callback),
}))

// ACL 已下线(2026-11 用户裁决):本页只做嵌入预览 + 知识库清单展示
type Kb = { id: string; name: string; docCount: number }
type State = {
  kbs: Ref<Kb[]>
  frameKey: Ref<string>
  frameSrc: Ref<string>
  frameOpen: Ref<boolean>
  reopenFrame: () => void
  loading: Ref<boolean>
  error: Ref<string>
  load: () => Promise<void>
}
const KG = '/ai-modeler/api/packs/knowledge_graph'
const kbA: Kb = { id: 'kb-a', name: '公开资料', docCount: 3 }
const kbB: Kb = { id: 'kb-b', name: '内部资料', docCount: 1 }
let scope: EffectScope
function setup() {
  const component = EmbedTestApp as unknown as {
    setup: (props: object, context: { expose: (value?: unknown) => void }) => State
  }
  return scope.run(() => component.setup({}, { expose: () => {} }))!
}
function serveKbs() {
  boundary.get.mockImplementation(async (url: string) => {
    if (url === `${KG}/kbs`) return { data: { items: [
      { id: kbA.id, name: kbA.name, doc_count: kbA.docCount },
      { id: kbB.id, name: kbB.name, docCount: kbB.docCount },
    ] } }
    throw new Error(`Unexpected URL: ${url}`)
  })
}
async function loaded() {
  serveKbs()
  const state = setup()
  await boundary.mounted[0]()
  return state
}
function deferredRequest() {
  let resolve!: (response: { data: unknown }) => void
  const promise = new Promise<{ data: unknown }>(res => { resolve = res })
  return { promise, resolve }
}
beforeEach(() => {
  vi.resetAllMocks()
  boundary.mounted.length = 0
  boundary.unmounting.length = 0
  scope = effectScope()
  vi.stubGlobal('localStorage', { getItem: vi.fn(() => 'test-token') })
  vi.stubGlobal('window', new EventTarget())
})
afterEach(() => {
  scope.stop()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('EmbedTestApp embed preview (ACL removed)', () => {
  it('collapses the embed preview when the child window sends CLOSE', async () => {
    const state = await loaded()
    expect(state.frameOpen.value).toBe(true)
    window.dispatchEvent(new MessageEvent('message', {
      data: { v: 1, src: 'child', type: 'CLOSE', payload: {} },
    }))
    expect(state.frameOpen.value).toBe(false)
    state.reopenFrame()
    expect(state.frameOpen.value).toBe(true)
    // 卸载时移除监听(不泄漏到其他页面)
    expect(boundary.unmounting.length).toBeGreaterThan(0)
    boundary.unmounting.forEach(fn => fn())
    window.dispatchEvent(new MessageEvent('message', {
      data: { v: 1, src: 'child', type: 'CLOSE', payload: {} },
    }))
    expect(state.frameOpen.value).toBe(true)
  })
  it('does not load before mount', () => {
    const state = setup()
    expect(state.frameKey.value).toBe('')
    expect(boundary.get).not.toHaveBeenCalled()
    expect(boundary.mounted).toHaveLength(1)
  })
  it('loads the KB list through the gateway and maps doc counts (snake/camel)', async () => {
    const state = await loaded()
    expect(boundary.get.mock.calls.map(call => call[0])).toEqual([`${KG}/kbs`])
    expect(state.kbs.value).toEqual([kbA, kbB])
    expect(state.frameKey.value).not.toBe('')
    expect(state.error.value).toBe('')
  })
  it('embeds without identity query params (ACL gone)', async () => {
    const state = await loaded()
    expect(state.frameSrc.value).toBe('/ai-modeler/?embed=true&hostless=1&packs=knowledge_graph')
  })
  it('adds the existing auth token to API requests', () => {
    setup()
    const request = { headers: {} as Record<string, string> }
    boundary.authorize.mock.calls[0][0](request)
    expect(request.headers.Authorization).toBe('Bearer test-token')
  })
})

describe('EmbedTestApp loading and failure feedback', () => {
  it('shows loading until the KB list resolves and only then creates the frame', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValueOnce(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    await Promise.resolve()
    expect(state.loading.value).toBe(true)
    expect(state.error.value).toBe('')
    expect(state.frameKey.value).toBe('')
    request.resolve({ data: { items: [{ id: kbB.id, name: kbB.name, doc_count: kbB.docCount }] } })
    await pending
    expect(state.loading.value).toBe(false)
    expect(state.error.value).toBe('')
    expect(state.frameKey.value).not.toBe('')
  })
  it('treats an actual empty KB list as success', async () => {
    boundary.get.mockResolvedValueOnce({ data: { items: [] } })
    const state = setup()
    await boundary.mounted[0]()
    expect(state.kbs.value).toEqual([])
    expect(state.error.value).toBe('')
    expect(state.loading.value).toBe(false)
    expect(state.frameKey.value).not.toBe('')
    expect(boundary.get).toHaveBeenCalledTimes(1)
  })
  it('reports a failed list request', async () => {
    boundary.get.mockRejectedValue(new Error('network unavailable'))
    const state = setup()
    await boundary.mounted[0]()
    expect(state.error.value).toBe('无法加载知识库清单，请检查服务后重试')
    expect(state.loading.value).toBe(false)
    expect(state.kbs.value).toEqual([])
    expect(state.frameKey.value).toBe('')
  })
  it('can retry after failure and clear the error', async () => {
    boundary.get.mockRejectedValueOnce(new Error('offline'))
    const state = setup()
    await boundary.mounted[0]()
    expect(state.error.value).not.toBe('')
    serveKbs()
    await state.load()
    expect(state.error.value).toBe('')
    expect(state.kbs.value).toHaveLength(2)
    expect(state.frameKey.value).not.toBe('')
  })
  it('clears stale KB data and the preview if refresh fails', async () => {
    const state = await loaded()
    boundary.get.mockRejectedValue(new Error('refresh failed'))
    await state.load()
    expect(state.kbs.value).toEqual([])
    expect(state.frameKey.value).toBe('')
    expect(state.error.value).not.toBe('')
  })
  it('does not duplicate requests while loading', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValueOnce(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    await state.load() // loading 中重复调用应被吞掉
    expect(boundary.get).toHaveBeenCalledTimes(1)
    request.resolve({ data: { items: [] } })
    await pending
    expect(state.loading.value).toBe(false)
  })
})
