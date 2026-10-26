import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope, type Ref } from 'vue'
import EmbedTestApp from './EmbedTestApp.vue'

const boundary = vi.hoisted(() => ({
  get: vi.fn(),
  authorize: vi.fn(),
  mounted: [] as Array<() => Promise<void>>,
}))
vi.mock('axios', () => ({ default: { create: () => ({
  get: boundary.get,
  interceptors: { request: { use: boundary.authorize } },
}) } }))
vi.mock('vue', async importOriginal => ({
  ...await importOriginal<typeof import('vue')>(),
  useSSRContext: () => ({ modules: new Set<string>() }),
  onMounted: (callback: () => Promise<void>) => boundary.mounted.push(callback),
}))

type KbAcl = { id: string; name: string; users: string[]; roles: string[] }
type State = {
  userId: Ref<string | undefined>
  pickedRoles: Ref<string[]>
  userOptions: Ref<Array<{ value: string; label: string }>>
  roleOptions: Ref<Array<{ value: string; label: string }>>
  kbAcl: Ref<KbAcl[]>
  dirty: Ref<boolean>
  frameKey: Ref<string>
  frameSrc: Ref<string>
  loading: Ref<boolean>
  error: Ref<string>
  activeTab: Ref<string>
  load: () => Promise<void>
  reload: () => void
  allowed: (kb: KbAcl) => boolean
  why: (kb: KbAcl) => string
}
const KG = '/ai-modeler/api/packs/knowledge_graph'
const openKb: KbAcl = { id: 'open', name: '公开资料', users: [], roles: [] }
const privateKb: KbAcl = { id: 'private', name: '内部资料', users: ['u1'], roles: ['r1'] }
const options = {
  users: [{ id: 'u1', name: '张三' }, { id: 'u2', name: 'u2' }],
  roles: [{ id: 'r1', name: '检定员' }, { id: 'r2', name: '' }],
}
let scope: EffectScope
function setup() {
  const component = EmbedTestApp as unknown as {
    setup: (props: object, context: { expose: (value?: unknown) => void }) => State
  }
  return scope.run(() => component.setup({}, { expose: () => {} }))!
}
function serveKbs() {
  boundary.get.mockImplementation(async (url: string) => {
    if (url === `${KG}/kbs`) return { data: { items: [openKb, privateKb] } }
    const kb = [openKb, privateKb].find(item => url === `${KG}/kbs/${item.id}/acl`)
    if (!kb) throw new Error(`Unexpected URL: ${url}`)
    return { data: { ...kb, options } }
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
  scope = effectScope()
  vi.stubGlobal('localStorage', { getItem: vi.fn(() => 'test-token') })
})
afterEach(() => {
  scope.stop()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('EmbedTestApp identity and ACL preview', () => {
  it('starts on the identity tab without loading before mount', () => {
    const state = setup()
    expect(state.activeTab.value).toBe('identity')
    expect(state.userId.value).toBeUndefined()
    expect(state.pickedRoles.value).toEqual([])
    expect(state.dirty.value).toBe(false)
    expect(state.frameKey.value).toBe('')
    expect(boundary.get).not.toHaveBeenCalled()
    expect(boundary.mounted).toHaveLength(1)
  })
  it('loads subjects and ACLs through the gateway and retains display labels', async () => {
    const state = await loaded()
    expect(boundary.get.mock.calls.map(call => call[0])).toEqual([
      `${KG}/kbs`, `${KG}/kbs/open/acl`, `${KG}/kbs/private/acl`,
    ])
    expect(state.kbAcl.value).toEqual([openKb, privateKb])
    expect(state.userOptions.value).toEqual([
      { value: 'u1', label: '张三（u1）' }, { value: 'u2', label: 'u2' },
    ])
    expect(state.roleOptions.value).toEqual([
      { value: 'r1', label: '检定员（r1）' }, { value: 'r2', label: 'r2' },
    ])
  })
  it('adds the existing auth token to API requests', () => {
    setup()
    const request = { headers: {} as Record<string, string> }
    boundary.authorize.mock.calls[0][0](request)
    expect(request.headers.Authorization).toBe('Bearer test-token')
  })
  it('keeps every KB open when no identity is supplied', () => {
    const state = setup()
    expect(state.allowed(privateKb)).toBe(true)
    expect(state.why(privateKb)).toBe('开放')
  })
  it('keeps an empty ACL open for a selected identity', () => {
    const state = setup()
    state.userId.value = 'unmatched'
    state.pickedRoles.value = ['unmatched']
    expect(state.allowed(openKb)).toBe(true)
    expect(state.why(openKb)).toBe('未配权限')
  })
  it('matches an authorized user before considering role matches', () => {
    const state = setup()
    state.userId.value = 'u1'
    state.pickedRoles.value = ['r1']
    expect(state.allowed(privateKb)).toBe(true)
    expect(state.why(privateKb)).toBe('用户命中')
  })
  it('matches any selected role even with an unmatched user', () => {
    const state = setup()
    state.userId.value = 'u2'
    state.pickedRoles.value = ['r2', 'r1']
    expect(state.allowed(privateKb)).toBe(true)
    expect(state.why(privateKb)).toBe('角色「r1」')
  })
  it('supports roles without a user', () => {
    const state = setup()
    state.pickedRoles.value = ['r1']
    expect(state.allowed(privateKb)).toBe(true)
    expect(state.why(privateKb)).toBe('角色「r1」')
  })
  it('denies an identity that matches neither users nor roles', () => {
    const state = setup()
    state.userId.value = 'u2'
    state.pickedRoles.value = ['r2']
    expect(state.allowed(privateKb)).toBe(false)
    expect(state.why(privateKb)).toBe('未授权')
  })
})

describe('EmbedTestApp loading and failure feedback', () => {
  it('shows loading until every ACL has resolved and only then creates the frame', async () => {
    const request = deferredRequest()
    boundary.get.mockResolvedValueOnce({ data: { items: [privateKb] } }).mockReturnValueOnce(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    await Promise.resolve()
    expect(state.loading.value).toBe(true)
    expect(state.error.value).toBe('')
    expect(state.frameKey.value).toBe('')
    request.resolve({ data: { ...privateKb, options } })
    await pending
    expect(state.loading.value).toBe(false)
    expect(state.error.value).toBe('')
    expect(state.frameKey.value).not.toBe('')
  })
  it('treats an actual empty KB list as success', async () => {
    boundary.get.mockResolvedValueOnce({ data: { items: [] } })
    const state = setup()
    await boundary.mounted[0]()
    expect(state.kbAcl.value).toEqual([])
    expect(state.userOptions.value).toEqual([])
    expect(state.roleOptions.value).toEqual([])
    expect(state.error.value).toBe('')
    expect(state.loading.value).toBe(false)
    expect(state.frameKey.value).not.toBe('')
    expect(boundary.get).toHaveBeenCalledTimes(1)
  })
  it.each(['list', 'acl'] as const)('reports a failed %s request rather than a healthy empty KB list', async failure => {
    if (failure === 'acl') {
      boundary.get.mockResolvedValueOnce({ data: { items: [openKb, privateKb] } })
        .mockResolvedValueOnce({ data: { ...openKb, options } })
    }
    boundary.get.mockRejectedValue(new Error('network unavailable'))
    const state = setup()
    await boundary.mounted[0]()
    expect(state.error.value).toBe('无法加载身份与权限范围，请检查服务后重试')
    expect(state.loading.value).toBe(false)
    expect(state.kbAcl.value).toEqual([])
    expect(state.userOptions.value).toEqual([])
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
    expect(state.kbAcl.value).toHaveLength(2)
    expect(state.frameKey.value).not.toBe('')
  })
  it('clears stale permission data and the preview if refresh fails', async () => {
    const state = await loaded()
    boundary.get.mockRejectedValue(new Error('refresh failed'))
    await state.load()
    expect(state.kbAcl.value).toEqual([])
    expect(state.userOptions.value).toEqual([])
    expect(state.roleOptions.value).toEqual([])
    expect(state.frameKey.value).toBe('')
    expect(state.error.value).not.toBe('')
  })
  it('does not duplicate requests while loading', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValueOnce(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    await state.load()
    expect(boundary.get).toHaveBeenCalledTimes(1)
    request.resolve({ data: { items: [] } })
    await pending
    expect(state.loading.value).toBe(false)
  })
})

describe('EmbedTestApp explicit identity reload', () => {
  it('keeps the applied iframe URL unchanged until reload while updating the ACL preview', async () => {
    const state = await loaded()
    const originalSrc = state.frameSrc.value
    const originalKey = state.frameKey.value
    state.userId.value = '用户 & one'
    state.pickedRoles.value = ['r2', 'r1']
    state.dirty.value = true
    expect(state.frameSrc.value).toBe(originalSrc)
    expect(state.frameKey.value).toBe(originalKey)
    expect(state.allowed(privateKb)).toBe(true)
    state.reload()
    const url = new URL(state.frameSrc.value, 'https://test.invalid')
    expect(url.pathname).toBe('/ai-modeler/')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      embed: 'true', hostless: '1', packs: 'knowledge_graph', userId: '用户 & one', roles: 'r2,r1',
    })
    expect(state.dirty.value).toBe(false)
    expect(state.frameKey.value).not.toBe(originalKey)
  })
  it('removes cleared identity parameters on reload', async () => {
    const state = await loaded()
    state.userId.value = 'u1'
    state.pickedRoles.value = ['r1']
    state.reload()
    state.userId.value = undefined
    state.pickedRoles.value = []
    state.dirty.value = true
    state.reload()
    expect(state.frameSrc.value).toBe('/ai-modeler/?embed=true&hostless=1&packs=knowledge_graph')
    expect(state.dirty.value).toBe(false)
  })
  it('rebuilds the frame even for reloads within the same millisecond', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(1000)
    const state = await loaded()
    const first = state.frameKey.value
    state.reload()
    expect(state.frameKey.value).not.toBe(first)
  })
})
