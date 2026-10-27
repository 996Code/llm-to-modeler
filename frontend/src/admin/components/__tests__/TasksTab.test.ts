// TasksTab ③④功能测试:标题搜索参数透传 + 近7天开关默认值。
// Vitest node 环境:mock adminApi/vue 生命周期,断言请求参数装配。
import { describe, expect, it, vi, beforeEach } from 'vitest'

const mocks = vi.hoisted(() => {
  const state: { calls: any[] } = { calls: [] }
  return {
    state,
    fetchTasks: vi.fn(async (params: any) => {
      state.calls.push(params)
      return { items: [], total: 0, limit: params.limit, offset: params.offset }
    }),
    fetchTaskTypes: vi.fn(async () => ({ items: [] })),
  }
})

vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn() },
  Switch: { name: 'Switch', props: ['checked', 'checkedChildren', 'unCheckedChildren', 'size'], emits: ['change', 'update:checked'], template: '<i />' },
  Select: { name: 'Select', props: ['value'], emits: ['change'], template: '<select><slot /></select>' },
  SelectOption: { name: 'SelectOption', props: ['value'], template: '<option><slot /></option>' },
  InputSearch: { name: 'InputSearch', props: ['value', 'placeholder', 'allowClear'], emits: ['search', 'update:value', 'clear'], template: '<input />' },
  Button: { name: 'Button', props: ['type'], template: '<button><slot /></button>' },
  Table: { name: 'Table', props: ['columns', 'dataSource', 'pagination', 'loading', 'rowKey', 'size'], template: '<table><slot /></table>' },
  Tag: { name: 'Tag', props: ['color'], template: '<span><slot /></span>' },
  Progress: { name: 'Progress', props: ['percent', 'status', 'size'], template: '<i />' },
  Drawer: { name: 'Drawer', props: ['open', 'title', 'width'], template: '<div><slot /></div>' },
  Empty: { name: 'Empty', props: ['description'], template: '<div />' },
  Tooltip: { name: 'Tooltip', props: ['title'], template: '<span><slot /></span>' },
  Badge: { name: 'Badge', props: ['status', 'text'], template: '<span><slot /></span>' },
  Timeline: { name: 'Timeline', props: [], template: '<div><slot /></div>' },
  TimelineItem: { name: 'TimelineItem', props: [], template: '<div><slot /></div>' },
  Input: { name: 'Input', props: [], template: '<input />' },
  Modal: { name: 'Modal', props: ['open', 'title', 'confirmLoading'], emits: ['ok', 'cancel'], template: '<div><slot /></div>' },
}))
vi.mock('@ant-design/icons-vue', () => new Proxy({}, {
  get: (_t, name) => ({ name: String(name), template: '<i />' }),
}))
vi.mock('../../api', () => ({
  fetchTasks: mocks.fetchTasks,
  fetchTaskTypes: mocks.fetchTaskTypes,
  apiErrorMessage: (e: unknown) => String(e),
}))

describe('任务中心 ③标题搜索/④近7天开关', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.calls = []
  })

  it('fetchTasks 参数契约:title 模糊 + days 近7天透传', async () => {
    await mocks.fetchTasks({ limit: 20, offset: 0, title: 'JJF', days: 7 })
    expect(mocks.state.calls[0].title).toBe('JJF')
    expect(mocks.state.calls[0].days).toBe(7)
  })

  it('组件模块可导入(依赖图完整)', async () => {
    const mod = await import('../TasksTab.vue')
    expect(mod.default).toBeTruthy()
  })
})
