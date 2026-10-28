import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope, type Ref } from 'vue'
import { readFileSync } from 'node:fs'
import Index from '../index.vue'
import type { MetrologyConfig } from '../../../metrologyApi'

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

  it('polished 能力行编辑:打开抽屉改追问文案,保存整档落库', async () => {
    const state = await loaded()
    await state.openCapEditor({ status: 'polished', kind: 'chatbi',
      scenario_key: 'progress', tool: 'query_progress', label: '进度查询' })
    expect(state.editorOpen.value).toBe(true)
    expect(state.currentKey.value).toBe('progress')

    state.draft.value.scenarios.progress.required.question = '请提供委托单号(回执单上可查)'
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
    state.draft.value.scenarios.progress.label = '改坏了'
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
    expect(state.draft.value.scenarios.query_workload).toBeTruthy()

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
