import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope, type Ref } from 'vue'
import MetrologyTestApp from './MetrologyTestApp.vue'

const boundary = vi.hoisted(() => ({
  get: vi.fn(), success: vi.fn(), error: vi.fn(),
  mounted: [] as Array<() => Promise<void>>,
  unmounting: [] as Array<() => void>,
}))
vi.mock('../admin/api', () => ({ adminApi: { get: boundary.get } }))
vi.mock('ant-design-vue', () => ({ message: { success: boundary.success, error: boundary.error } }))
vi.mock('vue', async importOriginal => ({
  ...await importOriginal<typeof import('vue')>(),
  useSSRContext: () => ({ modules: new Set<string>() }),
  onMounted: (callback: () => Promise<void>) => boundary.mounted.push(callback),
  onBeforeUnmount: (callback: () => void) => boundary.unmounting.push(callback),
}))

function readyEnv() {
  return {
    kbMapping: { purposes: ['贸易结算'], mandatoryCheckKb: '强检目录', regulationKb: '检定规程' },
    kbs: [
      { name: '强检目录', exists: true, docs: 2, imported: 1 },
      { name: '检定规程', exists: true, docs: 3, imported: 3 },
    ],
    progress: {
      env_configured: true,
      datasource: { name: '进度查询', active: true, dbType: 'mcp', host: 'metrology', tool: 'query_progress' },
      mcp: { code: 'metrology', status: 'connected', enabled: true, tools: ['query_progress'] },
    },
    orderFixtures: [{ order: 'TEST-001', desc: '模拟订单' }],
    errors: [] as string[],
  }
}
type Env = ReturnType<typeof readyEnv>
type State = {
  env: Ref<Env | null>
  loading: Ref<boolean>
  error: Ref<string>
  checkedAt: Ref<string>
  dependencies: Ref<Array<{ name: string; ok: boolean; detail: string }>>
  environmentReady: Ref<boolean>
  environmentStatus: Ref<string>
  load: () => Promise<void>
  copyQuestion: (question: string) => Promise<void>
}
let scope: EffectScope
const writeText = vi.fn<(text: string) => Promise<void>>()

// Call the compiled SFC setup without rendering. Keep Vue reactivity and
// AbortController real; replace only lifecycle registration and external I/O.
function setup() {
  const component = MetrologyTestApp as unknown as {
    setup: (props: object, context: { expose: (value?: unknown) => void }) => State
  }
  return scope.run(() => component.setup({}, { expose: () => {} }))!
}
function deferredRequest() {
  let resolve!: (response: { data: unknown }) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<{ data: unknown }>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
async function loaded(data: unknown = readyEnv()) {
  boundary.get.mockResolvedValue({ data })
  const state = setup()
  await boundary.mounted[0]()
  return state
}
beforeEach(() => {
  vi.resetAllMocks()
  boundary.mounted.length = 0
  boundary.unmounting.length = 0
  scope = effectScope()
  writeText.mockReset().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
})
afterEach(() => {
  boundary.unmounting.forEach(callback => callback())
  scope.stop()
  vi.unstubAllGlobals()
})

describe('MetrologyTestApp dependency readiness', () => {
  it('starts unchecked without requesting before mount', () => {
    const state = setup()
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('未检查')
    expect(state.dependencies.value).toEqual([])
    expect(boundary.get).not.toHaveBeenCalled()
    expect(boundary.mounted).toHaveLength(1)
    expect(boundary.unmounting).toHaveLength(1)
  })
  it('requires both imported KBs, the MCP tool, and a matching active datasource', async () => {
    const state = await loaded()
    expect(state.dependencies.value).toEqual([
      { name: '强检目录', ok: true, detail: '1 / 2 篇文档已导入' },
      { name: '检定规程', ok: true, detail: '3 / 3 篇文档已导入' },
      { name: '业务平台 MCP', ok: true, detail: 'metrology · connected' },
      { name: '进度查询数据源', ok: true, detail: '进度查询 · query_progress' },
    ])
    expect(state.environmentReady.value).toBe(true)
    expect(state.environmentStatus.value).toBe('依赖就绪')
  })
  it.each([
    ['inactive', { active: false }],
    ['wrong database type', { dbType: 'postgresql' }],
    ['wrong MCP host', { host: 'other-server' }],
    ['wrong tool', { tool: 'other_tool' }],
  ])('rejects a datasource with %s even when the other three conditions pass', async (_name, change) => {
    const data = readyEnv()
    Object.assign(data.progress.datasource, change)
    const state = await loaded(data)
    expect(state.dependencies.value[3].ok).toBe(false)
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('依赖待检查')
  })
  it.each([
    ['disconnected', { status: 'disconnected' }],
    ['disabled', { enabled: false }],
    ['missing query_progress', { tools: ['other_tool'] }],
    ['empty tool list', { tools: [] }],
  ])('rejects an MCP server that is %s', async (_name, change) => {
    const data = readyEnv()
    Object.assign(data.progress.mcp, change)
    const state = await loaded(data)
    expect(state.dependencies.value[2].ok).toBe(false)
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('依赖待检查')
  })
  it('accepts query_progress alongside other MCP tools', async () => {
    const data = readyEnv()
    data.progress.mcp.tools = ['other_tool', 'query_progress']
    expect((await loaded(data)).environmentReady.value).toBe(true)
  })
  it.each([0, 1])('rejects missing KB %i even if its imported counter is positive', async index => {
    const data = readyEnv()
    data.kbs[index].exists = false
    const state = await loaded(data)
    expect(state.dependencies.value[index]).toMatchObject({ ok: false, detail: '知识库未创建' })
    expect(state.environmentReady.value).toBe(false)
  })
  it.each([0, 1])('rejects KB %i with documents but no completed imports', async index => {
    const data = readyEnv()
    data.kbs[index].imported = 0
    const state = await loaded(data)
    expect(state.dependencies.value[index].ok).toBe(false)
    expect(state.environmentReady.value).toBe(false)
  })
  it.each([0, 1, 3])('does not declare readiness with %i KB entries', async count => {
    const data = readyEnv()
    data.kbs = Array.from({ length: count }, (_, index) => ({ ...data.kbs[0], name: `KB-${index}` }))
    const state = await loaded(data)
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('依赖待检查')
  })
  it.each(['datasource', 'mcp'] as const)('rejects a missing %s', async key => {
    const data = readyEnv()
    const state = await loaded({ ...data, progress: { ...data.progress, [key]: null } })
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('依赖待检查')
    expect(state.dependencies.value[key === 'mcp' ? 2 : 3]).toMatchObject({
      ok: false, detail: key === 'mcp' ? '未注册或状态不可用' : '数据源不存在或不可用',
    })
  })
  it('explains an unconfigured datasource', async () => {
    const data = readyEnv()
    const state = await loaded({ ...data, progress: { ...data.progress, env_configured: false, datasource: null } })
    expect(state.dependencies.value[3]).toMatchObject({ ok: false, detail: '未配置数据源' })
    expect(state.environmentReady.value).toBe(false)
  })
  it('shows disabled explanations for MCP and datasource', async () => {
    const data = readyEnv()
    data.progress.mcp.enabled = false
    data.progress.datasource.active = false
    const state = await loaded(data)
    expect(state.dependencies.value[2].detail).toBe('metrology · 已停用')
    expect(state.dependencies.value[3].detail).toBe('进度查询 · 已停用')
  })
  it('rejects API errors despite otherwise healthy dependencies', async () => {
    const data = readyEnv()
    data.errors = ['依赖探测失败']
    const state = await loaded(data)
    expect(state.dependencies.value.every(item => item.ok)).toBe(true)
    expect(state.env.value?.errors).toEqual(['依赖探测失败'])
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('依赖待检查')
  })
  it('accepts an absent optional errors field', async () => {
    const { errors: _errors, ...data } = readyEnv()
    expect((await loaded(data)).environmentReady.value).toBe(true)
  })
  it.each([null, undefined])('does not report ready for an empty response (%s)', async data => {
    boundary.get.mockResolvedValue({ data })
    const state = setup()
    await boundary.mounted[0]()
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('未检查')
    expect(state.dependencies.value).toEqual([])
  })
})

describe('MetrologyTestApp loading and teardown', () => {
  it('shows checking during requests, uses the gateway and records the check time', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValue(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    expect(state.loading.value).toBe(true)
    expect(state.environmentStatus.value).toBe('检查中')
    expect(state.checkedAt.value).toBe('')
    expect(boundary.get).toHaveBeenCalledWith('/packs/metrology_assistant/test/env', {
      baseURL: '/ai-modeler/api', signal: expect.any(AbortSignal),
    })
    request.resolve({ data: readyEnv() })
    await pending
    expect(state.loading.value).toBe(false)
    expect(state.checkedAt.value).not.toBe('')
    expect(state.environmentStatus.value).toBe('依赖就绪')
  })
  it('does not start a second request while checking', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValue(request.promise)
    const state = setup()
    const pending = state.load()
    await state.load()
    expect(boundary.get).toHaveBeenCalledTimes(1)
    request.resolve({ data: readyEnv() })
    await pending
    expect(state.loading.value).toBe(false)
  })
  it('shows request failure instead of unchecked or ready and clears loading', async () => {
    boundary.get.mockRejectedValue(new Error('network unavailable'))
    const state = setup()
    await boundary.mounted[0]()
    expect(state.env.value).toBeNull()
    expect(state.error.value).toBe('无法获取环境状态，请检查服务后重试')
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('状态获取失败')
    expect(state.loading.value).toBe(false)
    expect(state.checkedAt.value).toBe('')
  })
  it('clears a previously healthy environment when refresh fails', async () => {
    const state = await loaded()
    boundary.get.mockRejectedValue(new Error('refresh unavailable'))
    await state.load()
    expect(state.env.value).toBeNull()
    expect(state.dependencies.value).toEqual([])
    expect(state.environmentReady.value).toBe(false)
    expect(state.environmentStatus.value).toBe('状态获取失败')
  })
  it('clears previous errors immediately on retry and can return to ready', async () => {
    boundary.get.mockRejectedValueOnce(new Error('first request failed'))
    const state = setup()
    await state.load()
    const request = deferredRequest()
    boundary.get.mockReturnValueOnce(request.promise)
    const pending = state.load()
    expect(state.error.value).toBe('')
    expect(state.environmentStatus.value).toBe('检查中')
    request.resolve({ data: readyEnv() })
    await pending
    expect(state.environmentReady.value).toBe(true)
    expect(state.environmentStatus.value).toBe('依赖就绪')
  })
  it('allows teardown before any request exists', () => {
    setup()
    expect(() => boundary.unmounting[0]()).not.toThrow()
    expect(boundary.get).not.toHaveBeenCalled()
  })
  it('aborts in-flight requests on teardown without reporting network failure', async () => {
    const request = deferredRequest()
    boundary.get.mockReturnValue(request.promise)
    const state = setup()
    const pending = boundary.mounted[0]()
    const signal = boundary.get.mock.calls[0][1].signal as AbortSignal
    signal.addEventListener('abort', () => request.reject(new Error('request cancelled')), { once: true })
    expect(signal.aborted).toBe(false)
    boundary.unmounting[0]()
    expect(signal.aborted).toBe(true)
    await pending
    expect(state.error.value).toBe('')
    expect(state.env.value).toBeNull()
    expect(state.loading.value).toBe(false)
    expect(state.environmentStatus.value).toBe('未检查')
    expect(boundary.error).not.toHaveBeenCalled()
  })
  it('aborts the newest controller after a completed check and refresh', async () => {
    const state = await loaded()
    const originalSignal = boundary.get.mock.calls[0][1].signal as AbortSignal
    const request = deferredRequest()
    boundary.get.mockReturnValueOnce(request.promise)
    const pending = state.load()
    const currentSignal = boundary.get.mock.calls[1][1].signal as AbortSignal
    currentSignal.addEventListener('abort', () => request.reject(new Error('request cancelled')), { once: true })
    expect(currentSignal).not.toBe(originalSignal)
    boundary.unmounting[0]()
    expect(currentSignal.aborted).toBe(true)
    expect(originalSignal.aborted).toBe(false)
    await pending
    expect(state.error.value).toBe('')
    expect(state.loading.value).toBe(false)
  })
})

describe('MetrologyTestApp clipboard feedback', () => {
  it('copies the exact question and reports success only after clipboard resolution', async () => {
    let finish!: () => void
    writeText.mockReturnValueOnce(new Promise<void>(resolve => { finish = resolve }))
    const state = setup()
    const question = '查询委托单 TEST-001 的检测进度'
    const pending = state.copyQuestion(question)
    expect(writeText).toHaveBeenCalledExactlyOnceWith(question)
    expect(boundary.success).not.toHaveBeenCalled()
    finish()
    await pending
    expect(boundary.success).toHaveBeenCalledExactlyOnceWith('测试问题已复制')
    expect(boundary.error).not.toHaveBeenCalled()
  })
  it('reports clipboard rejection without claiming success or rejecting the handler', async () => {
    writeText.mockRejectedValueOnce(new Error('permission denied'))
    await expect(setup().copyQuestion('压力表需要强制检定吗？')).resolves.toBeUndefined()
    expect(boundary.error).toHaveBeenCalledExactlyOnceWith('复制失败，请检查浏览器剪贴板权限')
    expect(boundary.success).not.toHaveBeenCalled()
  })
  it('reports unavailable clipboard support without throwing', async () => {
    vi.stubGlobal('navigator', {})
    await expect(setup().copyQuestion('测试问题')).resolves.toBeUndefined()
    expect(boundary.error).toHaveBeenCalledExactlyOnceWith('复制失败，请检查浏览器剪贴板权限')
    expect(boundary.success).not.toHaveBeenCalled()
  })
})
