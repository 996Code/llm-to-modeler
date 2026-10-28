import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, type EffectScope, type Ref } from 'vue'
import { readFileSync } from 'node:fs'
import Index from '../index.vue'
import type { MetrologyConfig, MetrologyScenario } from '../../../metrologyApi'

const boundary = vi.hoisted(() => ({
  get: vi.fn(), put: vi.fn(), post: vi.fn(), datasources: vi.fn(),
  success: vi.fn(), error: vi.fn(), mounted: [] as Array<() => Promise<void>>,
}))
vi.mock('../../../metrologyApi', () => ({
  metrologyApi: { get: boundary.get, put: boundary.put, post: boundary.post },
}))
vi.mock('../../../api', () => ({ chatbiApi: { get: boundary.datasources } }))
vi.mock('ant-design-vue', () => ({ message: { success: boundary.success, error: boundary.error } }))
vi.mock('vue', async importOriginal => ({
  ...await importOriginal<typeof import('vue')>(),
  useSSRContext: () => ({ modules: new Set<string>() }),
  onMounted: (callback: () => Promise<void>) => boundary.mounted.push(callback),
}))

const saved: MetrologyConfig = {
  revision: 1,
  factory: false,
  content: { scenarios: {
    progress: {
      label: '进度查询', intent: '查询检测进度',
      required: { slot: 'order_no', header: '委托单号', question: '请提供委托单号', options: [] },
      action: { kind: 'chatbi', datasource_id: 'business', tool: 'query_progress', params: { order_no: 'order_no' } },
    },
  } },
}
type State = {
  config: Ref<MetrologyConfig>
  draft: Ref<MetrologyConfig['content']>
  currentScenario: Readonly<Ref<MetrologyScenario | null>>
  capabilityKey: (cap: any) => string
  serverTools: (cfg: MetrologyScenario) => string[]
  setParams: (cfg: MetrologyScenario, event: Event) => void
  setJson: (cfg: MetrologyScenario, field: string, event: Event) => void
  jsonText: (cfg: MetrologyScenario, field: string) => string
  parameterRows: Readonly<Ref<Array<{ name: string; required: boolean; type: string; source: string; hint: string }>>>
  datasources: Ref<Array<any>>
  pickDatasource: (cfg: MetrologyScenario, event: Event) => void
  saveError: Ref<string>
  dry: { text: string; execute: boolean; current: unknown; error: string }
  tab: Ref<string>
  currentHelp: Readonly<Ref<string>>
  routing: Ref<Array<{ status: string }>>
  routingSummary: Readonly<Ref<string>>
  editorOpen: Ref<boolean>
  editorCap: Ref<unknown>
  currentKey: Ref<string>
  openCapEditor: (cap: any) => Promise<void>
  cancelEdit: () => void
  drawerSave: () => Promise<void>
  resetToLatest: (cap: any) => Promise<void>
  reload: () => Promise<void>
  loadRouting: (force?: boolean) => Promise<void>
  runDry: () => Promise<void>
}
let scope: EffectScope
function setup() {
  const component = Index as unknown as {
    setup: (props: object, context: { expose: (value?: unknown) => void }) => State
  }
  return scope.run(() => component.setup({}, { expose: () => {} }))!
}
async function loaded() {
  const state = setup()
  await boundary.mounted[0]()
  return state
}
beforeEach(() => {
  vi.resetAllMocks()
  boundary.mounted.length = 0
  scope = effectScope()
  boundary.get.mockImplementation(async (url: string) => {
    if (url === '/config') return { data: structuredClone(saved) }
    if (url === '/config/routing') return { data: { capabilities: [], servers: [] } }
    throw new Error(`Unexpected GET ${url}`)
  })
  boundary.datasources.mockResolvedValue({ data: { items: [] } })
  boundary.put.mockResolvedValue({ data: { revision: 2 } })
})
afterEach(() => scope.stop())

describe('智能客服能力路由(编辑抽屉版)', () => {
  it('加载配置与路由总览,不再请求历史', async () => {
    const state = await loaded()
    expect(state.draft.value).toEqual(saved.content)
    expect(state.draft.value).not.toBe(state.config.value.content)
    expect(boundary.get.mock.calls.map(([url]) => url)).toEqual(['/config', '/config/routing'])
  })

  it('仅显式刷新重探测声明，初次加载和保存后不强制刷新', async () => {
    const state = await loaded()
    expect(boundary.get.mock.calls.filter(([url]) => url === '/config/routing')).toEqual([['/config/routing']])
    await state.loadRouting(true)
    expect(boundary.get).toHaveBeenLastCalledWith('/config/routing', { params: { refresh: true } })
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    await state.drawerSave()
    expect(boundary.get).toHaveBeenLastCalledWith('/config/routing')
    await state.resetToLatest({ scenario_key: 'progress' })
    expect(boundary.get).toHaveBeenLastCalledWith('/config/routing')
  })

  it('polished 能力行编辑:打开抽屉改追问文案,保存整档落库', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi',
      scenario_key: 'progress', tool: 'query_progress', label: '进度查询' })
    expect(state.editorOpen.value).toBe(true)
    expect(state.currentKey.value).toBe('progress')

    state.currentScenario.value!.required.question = '请提供委托单号(回执单上可查)'
    await state.drawerSave()
    expect(boundary.put).toHaveBeenCalledWith('/config', {
      content: expect.objectContaining({ scenarios: expect.objectContaining({
        progress: expect.objectContaining({ required: expect.objectContaining({
          question: '请提供委托单号(回执单上可查)' }) }) }) }),
      comment: expect.stringContaining('能力路由编辑'),
    })
    expect(boundary.put.mock.calls[0][1]).not.toHaveProperty('expected_revision')
    expect(state.editorOpen.value).toBe(false)
    expect(boundary.success).toHaveBeenCalledWith('已保存,下一次对话生效')
  })

  it('取消编辑还原快照,不落库', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi',
      scenario_key: 'progress', tool: 'query_progress', label: '进度查询' })
    state.currentScenario.value!.label = '改坏了'
    state.cancelEdit()
    expect(state.draft.value.scenarios.progress.label).toBe('进度查询')
    expect(boundary.put).not.toHaveBeenCalled()
    expect(state.editorOpen.value).toBe(false)
  })

  it('auto 能力行创建覆盖:先按声明起草再编辑;行级重置放弃覆盖回到自动', async () => {
    const state = await loaded()
    boundary.post.mockResolvedValueOnce({ data: {
      key: 'query_workload',
      scenario: { label: '工作量', intent: '问工作量',
        required: { slot: 'category', header: '类别', question: '请提供类别', options: [] },
        action: { kind: 'chatbi', server: 'metrology', tool: 'query_workload',
          params: { category: 'category' }, card: '_table_card' } } } })
    await state.openCapEditor({ status: 'auto', kind: 'chatbi', server: 'metrology',
      tool: 'query_workload', label: '工作量', description: '统计检测工作量' })
    expect(boundary.post).toHaveBeenCalledWith('/config/capability-import/draft',
      { server: 'metrology', tool: 'query_workload' })
    expect(state.currentKey.value).toBe('query_workload')
    expect(state.currentScenario.value!.label).toBe('工作量')
    expect(state.draft.value.scenarios.query_workload).toBeUndefined()

    await state.resetToLatest({ status: 'polished', kind: 'chatbi',
      scenario_key: 'query_workload', tool: 'query_workload' })
    expect(state.draft.value.scenarios.query_workload).toBeUndefined()
    const putBody = boundary.put.mock.calls[0][1]
    expect(putBody.comment).toContain('重置为最新声明')
  })

  it('校验失败保留草稿并显示错误', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi',
      scenario_key: 'progress', tool: 'query_progress', label: '进度查询' })
    boundary.put.mockRejectedValue({ response: { status: 400, data: { detail: '请选择数据源' } } })
    await state.drawerSave()
    expect(state.saveError.value).toBe('校验失败: 请选择数据源')
    expect(state.draft.value.scenarios.progress).toBeTruthy()
  })

  it.each(['cancel', 'close'])('新建覆盖后 %s 不夹带到另一能力保存', async mode => {
    const state = await loaded()
    boundary.post.mockResolvedValueOnce({ data: { key: 'new', scenario: structuredClone(saved.content.scenarios.progress) } })
    await state.openCapEditor({ status: 'auto', kind: 'chatbi', server: 'other', tool: 'query_progress' })
    state.currentScenario.value!.label = '不应落库'
    if (mode === 'cancel') state.cancelEdit()
    else { state.editorOpen.value = false; await nextTick() }
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    await state.drawerSave()
    expect(Object.keys(boundary.put.mock.calls[0][1].content.scenarios)).toEqual(['progress'])
  })

  it('失败编辑不夹带到重置，删除最后覆盖且失败不改已保存配置', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    state.currentScenario.value!.label = '未保存'
    boundary.put.mockRejectedValueOnce(new Error('offline'))
    await state.drawerSave()
    expect(state.config.value.content).toEqual(saved.content)
    boundary.put.mockRejectedValueOnce(new Error('offline'))
    await state.resetToLatest({ scenario_key: 'progress' })
    expect(boundary.put.mock.calls[1][1].content.scenarios).toEqual({})
    expect(state.config.value.content).toEqual(saved.content)
  })

  it('非法参数 JSON 阻止保存而不是静默沿用旧值', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    state.setParams(state.currentScenario.value!, { target: { value: '{bad' } } as unknown as Event)
    await state.drawerSave()
    expect(boundary.put).not.toHaveBeenCalled()
    expect(state.saveError.value).toContain('工具参数映射')
  })

  it('同名工具按服务隔离身份，未绑定数据源也按 action.server 限定工具', async () => {
    const state = await loaded()
    const a = { server: 'a', tool: 'query' }, b = { server: 'b', tool: 'query' }
    expect(state.capabilityKey(a)).not.toBe(state.capabilityKey(b))
    state.routing.value = [
      { ...a, kind: 'chatbi', status: 'auto' },
      { server: 'b', tool: 'private_query', kind: 'chatbi', status: 'auto' },
    ] as any
    expect(state.serverTools({ ...saved.content.scenarios.progress, action: { kind: 'chatbi', server: 'a' } } as any)).toEqual(['query'])
  })

  it('保存保留顶层、场景、槽位及动作的未知配置字段', async () => {
    const fixture = structuredClone(saved) as any
    fixture.content.future = { enabled: true }
    fixture.content.scenarios.progress.future = { keep: true }
    fixture.content.scenarios.progress.required.future = ['x']
    fixture.content.scenarios.progress.action.future = { nested: 1 }
    boundary.get.mockImplementation(async (url: string) => ({ data: url === '/config' ? fixture : { capabilities: [], servers: [] } }))
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    state.currentScenario.value!.label = '新名称'
    await state.drawerSave()
    expect(boundary.put.mock.calls[0][1].content).toEqual({ ...fixture.content, scenarios: {
      progress: { ...fixture.content.scenarios.progress, label: '新名称' },
    } })
  })

  it('多参数显示完整 schema 的必填、类型、映射来源和提取提示', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    const cfg = state.currentScenario.value!
    cfg.action.input_schema = { type: 'object', required: ['order_no', 'year'], properties: {
      order_no: { type: 'string' }, year: { type: 'integer' }, verbose: { type: 'boolean' },
    } }
    cfg.action.params = { order_no: 'order_no', year: 'booking_year' }
    cfg._param_hints = { booking_year: '预约年份' }
    expect(state.parameterRows.value).toEqual([
      { name: 'order_no', required: true, type: 'string', source: 'order_no', hint: '' },
      { name: 'year', required: true, type: 'integer', source: 'booking_year', hint: '预约年份' },
      { name: 'verbose', required: false, type: 'boolean', source: 'verbose', hint: '' },
    ])
  })

  it('通用问答保留空 required，保存知识来源、查询模板与回答指引', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'kg', scenario_key: 'progress' })
    const cfg = state.currentScenario.value!
    cfg.required = {}
    cfg.action = { kind: 'kg', conclusion: 'answer', kb: '办事指南', query_template: '{question}', answer_prompt: '简洁列出步骤' }
    state.setJson(cfg, 'kb_rules', { target: { value: '[{"slot":"topic","in":["检定"],"kb":"检定指南"}]' } } as unknown as Event)
    await state.drawerSave()
    expect(boundary.put.mock.calls[0][1].content.scenarios.progress).toMatchObject({
      required: {}, action: { kind: 'kg', conclusion: 'answer', kb: '办事指南', query_template: '{question}', answer_prompt: '简洁列出步骤',
        kb_rules: [{ slot: 'topic', in: ['检定'], kb: '检定指南' }] },
    })
  })

  it.each(['input_schema', '_param_hints', 'kb_rules', 'notices'])('%s 非法结构保留输入并阻止保存，修正后可保存', async field => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    const cfg = state.currentScenario.value!
    const bad = field === 'kb_rules' ? '[{}]' : '{"required":1}'
    state.setJson(cfg, field, { target: { value: bad } } as unknown as Event)
    expect(state.jsonText(cfg, field)).toBe(bad)
    await state.drawerSave()
    expect(boundary.put).not.toHaveBeenCalled()
    state.setJson(cfg, field, { target: { value: field === 'kb_rules' ? '[]' : '{}' } } as unknown as Event)
    await state.drawerSave()
    expect(boundary.put).toHaveBeenCalledTimes(1)
  })

  it('更换到另一个 MCP 服务的数据源不保留旧服务工具和 schema', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi', scenario_key: 'progress' })
    const cfg = state.currentScenario.value!
    cfg.action.server = 'a'
    cfg.action.input_schema = { properties: { old: { type: 'string' } } }
    state.datasources.value = [{ id: 'b', name: 'B服务', dbType: 'mcp', host: 'b', database: 'query' }]
    state.pickDatasource(cfg, { target: { value: 'b' } } as unknown as Event)
    expect(cfg.action.server).toBe('b')
    expect(cfg.action.tool).toBe('query')
    expect(cfg.action.input_schema).toBeUndefined()
  })

  it('页面结构:两层 tab(能力路由/联调预演)+行内编辑抽屉,无独立润色场景页', () => {
    const source = readFileSync(new URL('../index.vue', import.meta.url), 'utf8')
    const template = source.split('<script setup')[0]
    expect(template.match(/<a-tab-pane key=/g)).toHaveLength(2)
    expect(template).toContain('key="routing"')
    expect(template).toContain('key="dryrun"')
    expect(template).not.toContain('key="scenarios"')
    expect(template).toContain('<a-drawer')
    expect(template).toContain('MCP 声明参考')
    expect(template).toContain('重置为最新声明')
    expect(template).toContain('@click="openCapEditor(record)"')
    expect(template).toContain('@confirm="resetToLatest(record)"')
    expect(template).not.toContain('删除覆盖')
    const visibleCopy = template.replace(/<[^>]*>/g, '')
    expect(visibleCopy).not.toMatch(/revision|\.env|CAS|call_logs|历史与回滚/)
    expect(source).not.toMatch(/expected_revision|\/config\/history|\/config\/rollback/)
  })
})
