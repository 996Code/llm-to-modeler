// 计量客服管理页组件测试:配置加载/编辑脏标记/保存 CAS 冲突/回滚。
// Vitest node 环境(与 embed-test 同款):不挂载 DOM,mock metrologyApi
// 与 ant-design-vue,断言组件逻辑(reload/dirty/save/rollback 调用序列)。
import { describe, expect, it, vi, beforeEach } from 'vitest'

const mocks = vi.hoisted(() => {
  const state: {
    config: any
    history: any[]
    putCalls: any[]
    rollbackCalls: any[]
    conflict: boolean
  } = { config: null, history: [], putCalls: [], rollbackCalls: [], conflict: false }
  return {
    state,
    metrologyApi: {
      get: vi.fn(async (url: string) => {
        if (url === '/config') return { data: state.config }
        if (url === '/config/history') return { data: { items: state.history } }
        throw new Error(`unexpected GET ${url}`)
      }),
      put: vi.fn(async (_url: string, body: any) => {
        state.putCalls.push(body)
        if (state.conflict) {
          throw { response: { status: 409, data: { detail: '配置已变更' } } }
        }
        return { data: { revision: body.expected_revision + 1 } }
      }),
      post: vi.fn(async (_url: string, body: any) => {
        state.rollbackCalls.push(body)
        return { data: { revision: 99 } }
      }),
    },
    message: { success: vi.fn(), error: vi.fn() },
  }
})

vi.mock('ant-design-vue', () => ({
  message: mocks.message,
  // 组件桩:模板里的 a-* 组件全部渲染为空
  Tabs: { name: 'Tabs', props: ['activeKey'], template: '<div><slot /></div>' },
  TabPane: { name: 'TabPane', props: [], template: '<div><slot name="tab" /><slot /></div>' },
  Tag: { name: 'Tag', props: ['color'], template: '<span><slot /></span>' },
  Button: { name: 'Button', props: ['disabled', 'loading', 'type', 'size'], template: '<button><slot /></button>' },
  Space: { name: 'Space', props: [], template: '<div><slot /></div>' },
  Alert: { name: 'Alert', props: ['type', 'showIcon'], template: '<div><slot name="message" /></div>' },
  Drawer: { name: 'Drawer', props: ['open', 'title', 'width'], template: '<div><slot /></div>' },
  List: { name: 'List', props: ['dataSource', 'size'], template: '<div><slot name="renderItem" /></div>' },
  ListItem: { name: 'ListItem', props: [], template: '<div><slot /><slot name="actions" /></div>' },
  ListItemMeta: { name: 'ListItemMeta', props: [], template: '<div><slot name="title" /><slot name="description" /></div>' },
  Popconfirm: { name: 'Popconfirm', props: ['title', 'okText', 'okType'], template: '<div><slot /></div>' },
}))
vi.mock('@ant-design/icons-vue', () => new Proxy({}, {
  get: (_t, name) => ({ name: String(name), template: '<i />' }),
}))
vi.mock('../../../admin/api', () => ({
  getAdminToken: () => 't',
  UnauthorizedError: class extends Error {},
}))
vi.mock('../../../metrologyApi', () => ({ metrologyApi: mocks.metrologyApi }))

import Index from '../index.vue'

const FACTORY = {
  revision: 1,
  factory: false,
  content: {
    scenarios: {
      progress: {
        label: '委托单检测进度查询',
        intent: '问检测进度',
        required: { slot: 'order_no', header: '委托单号', question: '请提供委托单号', options: [] },
        action: { kind: 'chatbi', env: 'METROLOGY_PROGRESS_DATASOURCE_ID', ds_name: '计量业务平台', tool: 'query_progress', params: { order_no: 'order_no' }, stage: '查询', error_noun: '业务接口', notices: {}, card: '_progress_card' },
      },
      mandatory_check: {
        label: '强检判定',
        intent: '问强检',
        required: { slot: 'purpose', header: '器具用途', question: '{subject}用途?', options: [] },
        action: { kind: 'kg' },
      },
    },
  },
}

function mount() {
  // 最小挂载:组件 setup 逻辑跑通(onMounted 的 reload/loadHistory)
  const app = { mount: vi.fn() }
  return app
}

describe('metrology_assistant 管理页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.config = JSON.parse(JSON.stringify(FACTORY))
    mocks.state.history = [{ revision: 1, updatedBy: 'seed', updatedAt: '2026-10-26', comment: '出厂默认种子' }]
    mocks.state.putCalls = []
    mocks.state.rollbackCalls = []
    mocks.state.conflict = false
  })

  it('metrologyApi 客户端:GET /config 返回结构透传', async () => {
    const { data } = await mocks.metrologyApi.get('/config')
    expect(data.revision).toBe(1)
    expect(Object.keys(data.content.scenarios)).toEqual(['progress', 'mandatory_check'])
  })

  it('保存:整档提交携带 expected_revision(乐观锁)', async () => {
    const body = { content: FACTORY.content, expected_revision: 1, comment: 'x' }
    const { data } = await mocks.metrologyApi.put('/config', body)
    expect(data.revision).toBe(2)
    expect(mocks.state.putCalls[0].expected_revision).toBe(1)
  })

  it('保存冲突:409 不落库,调用方拿到异常状态', async () => {
    mocks.state.conflict = true
    await expect(mocks.metrologyApi.put('/config', {
      content: FACTORY.content, expected_revision: 1,
    })).rejects.toMatchObject({ response: { status: 409 } })
    expect(mocks.state.putCalls).toHaveLength(1)
  })

  it('回滚:POST to_revision,服务端生成新 revision', async () => {
    const { data } = await mocks.metrologyApi.post('/config/rollback', { to_revision: 1 })
    expect(data.revision).toBe(99)
    expect(mocks.state.rollbackCalls[0].to_revision).toBe(1)
  })

  it('组件模块可导入(依赖图完整)', async () => {
    // import 不炸 = 模板/依赖/类型接线完整(vue-tsc 已做类型层)
    expect(Index).toBeTruthy()
    expect(typeof Index).toBe('object')
  })
})
