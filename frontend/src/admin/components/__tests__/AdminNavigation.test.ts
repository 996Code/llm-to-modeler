import { describe, expect, it, beforeEach, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick } from 'vue'
import * as Vue from 'vue'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'

const adminSource = (name: string) => readFileSync(new URL(`../${name}`, import.meta.url), 'utf8')
const readAdminApp = () => readFileSync(new URL('../../AdminApp.vue', import.meta.url), 'utf8')

type HostNode = {
  type: string
  props: Record<string, unknown>
  children: HostNode[]
  text?: string
  parent?: HostNode
  addEventListener?: (...args: unknown[]) => void
  removeEventListener?: (...args: unknown[]) => void
}

const makeHostNode = (type: string, text?: string): HostNode => ({
  type,
  props: {},
  children: [],
  text,
  addEventListener: () => {},
  removeEventListener: () => {},
})

const renderer = createRenderer<HostNode, HostNode>({
  createElement: (type) => makeHostNode(type),
  createText: (text) => makeHostNode('#text', text),
  createComment: (text) => makeHostNode('#comment', text),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => {
    node.children = [makeHostNode('#text', text)]
    node.children[0].parent = node
  },
  parentNode: (node) => node.parent || null,
  nextSibling: () => null,
  patchProp: (node, key, _oldValue, value) => { node.props[key] = value },
  insert: (child, parent, anchor) => {
    child.parent = parent
    if (!anchor) parent.children.push(child)
    else parent.children.splice(parent.children.indexOf(anchor), 0, child)
  },
  remove: (child) => {
    if (child.parent) child.parent.children = child.parent.children.filter((item) => item !== child)
  },
})

function textOf(node: HostNode): string {
  return node.type === '#text' || node.type === '#comment'
    ? node.text || ''
    : node.children.map(textOf).join('')
}

function passthrough(name: string, slots: ('default' | 'tab' | 'content' | 'rightExtra')[] = ['default']) {
  return defineComponent({
    name,
    setup(_props, { slots: slotMap }) {
      return () => h('div', {}, slots.flatMap((slot) => slotMap[slot]?.() || []))
    },
  })
}

function iconComponents() {
  return new Proxy({}, {
    get: (_target, name) => defineComponent({ name: String(name), setup: () => () => h('i') }),
  })
}

function antComponents() {
  return {
    Button: passthrough('Button'),
    Empty: passthrough('Empty'),
    Input: passthrough('Input'),
    Popover: passthrough('Popover', ['content', 'default']),
    Select: passthrough('Select'),
    SelectOption: passthrough('SelectOption'),
    Space: passthrough('Space'),
    Switch: passthrough('Switch'),
    Table: passthrough('Table'),
    Tabs: passthrough('Tabs', ['default', 'rightExtra']),
    TabPane: passthrough('TabPane', ['tab', 'default']),
    Tag: passthrough('Tag'),
    Tooltip: passthrough('Tooltip'),
    message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  }
}

function compileSfc(source: string, id: string, dependencies: Record<string, unknown>) {
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id, inlineTemplate: true })
  const compiled = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports: Record<string, unknown> = {}
  new Function('require', 'exports', compiled)(
    (name: string) => dependencies[name],
    exports,
  )
  return exports.default as any
}

function mountComponent(component: any, components: Record<string, any>, provide: Record<string, unknown> = {}) {
  const root: HostNode = makeHostNode('root')
  const app = renderer.createApp(component)
  for (const [name, value] of Object.entries(components)) app.component(name, value)
  for (const [key, value] of Object.entries(provide)) app.provide(key, value)
  app.mount(root)
  return { app, root, instance: (app as any)._instance }
}

const packsApi = {
  fetchPacks: vi.fn(async () => ({ items: [], stateFile: '', source: '' })),
  listMcpServers: vi.fn(async () => []),
  recheckPack: vi.fn(),
  setPackEnabled: vi.fn(),
}

const llmState: {
  configured: boolean
  putBody: unknown
  get: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
} = {
  configured: false,
  putBody: undefined,
  get: vi.fn(),
  put: vi.fn(),
}

const llmApi = {
  get: llmState.get,
  put: llmState.put,
  post: vi.fn(),
}

function loadPacksComponent() {
  const ant = antComponents()
  return compileSfc(adminSource('PacksTab.vue'), 'packs-tab-regression', {
    vue: Vue,
    'ant-design-vue': ant,
    '@ant-design/icons-vue': iconComponents(),
    '../api': packsApi,
    './LlmProviders.vue': passthrough('LlmProviders'),
    './McpServersPanel.vue': passthrough('McpServersPanel'),
    './PackSettingsDrawer.vue': passthrough('PackSettingsDrawer'),
  })
}

function loadLlmComponent() {
  const ant = antComponents()
  return compileSfc(adminSource('LlmProviders.vue'), 'llm-providers-regression', {
    vue: Vue,
    'ant-design-vue': ant,
    '@ant-design/icons-vue': iconComponents(),
    '../api': { adminApi: llmApi },
  })
}

describe('管理台导航和供应商回归', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    packsApi.fetchPacks.mockResolvedValue({ items: [], stateFile: '', source: '' })
    packsApi.listMcpServers.mockResolvedValue([])
    llmState.configured = false
    llmState.putBody = undefined
    llmState.get.mockResolvedValue({
      data: {
        configured: false,
        primary: { base_url: '', model: '', enabled: true, has_key: false },
        fallback: null,
      },
    })
    llmState.put.mockImplementation(async (_url: string, body: unknown) => {
      llmState.putBody = body
      return { data: {} }
    })
  })

  it('PacksTab 的 currentHelp 随 innerTab 切换到三个 tab 对应说明', () => {
    const source = adminSource('PacksTab.vue')
    expect(source).toContain("const innerTab = ref('packs')")
    expect(source).toContain('const currentHelp = computed(() => helpByTab[innerTab.value] || helpByTab.packs)')
    for (const title of ['插件说明', 'MCP 服务说明', '模型供应商说明']) {
      expect(source).toContain(`title: '${title}'`)
    }
    expect(source).toContain('v-model:activeKey="innerTab"')
  })

  it('LlmProviders 用后端 configured 渲染真实状态，保存请求不带 expected_revision', () => {
    const source = adminSource('LlmProviders.vue')
    expect(source).toContain(':status="configured ? \'success\' : \'default\'"')
    expect(source).toContain('class="lp-provider-heading"')
    expect(source).not.toContain('<a-tag')
    expect(source).toContain('configured.value = !!data.configured')
    expect(source).toContain("const body: any = { primary: { ...form.primary } }")
    expect(source).not.toContain('expected_revision')
  })

  it('LlmProviders 源码不再展示 .env 或 call_logs 实现文案', () => {
    const source = adminSource('LlmProviders.vue')
    expect(source).not.toMatch(/\.env|call_logs/)
    expect(source).toContain('configured.value = !!data.configured')
    expect(source).not.toContain('expected_revision')
  })

  it('AdminApp 的记录菜单只有一层 tabs，CallLogsTab 由 viewMode 控制且没有内部 radio', () => {
    const admin = readAdminApp()
    const logs = adminSource('CallLogsTab.vue')
    const navBlock = admin.match(/const navItems = computed\(\(\) => \[(?<body>[\s\S]*?)\n\]\)/)?.groups?.body || ''

    expect(navBlock).toContain("key: 'overview', label: '概览'")
    expect(navBlock).toContain("key: 'packs', label: '集成'")
    expect(navBlock).toContain("key: 'activity', label: '记录'")
    expect(navBlock).not.toContain('insights')
    expect(navBlock).not.toContain('convo')
    expect(admin.match(/<a-tabs\b/g) || []).toHaveLength(1)
    expect(admin.match(/<a-tab-pane key=/g) || []).toHaveLength(5)
    expect(admin).toContain('<CallLogsTab view-mode="detail" />')
    expect(admin).toContain('<CallLogsTab view-mode="stats" />')
    expect(admin).toContain('<CallLogsTab view-mode="audit" />')
    expect(admin).not.toMatch(/<CallLogsTab\s+v-if=/)
    expect(logs).toContain("defineProps<{ viewMode?: 'detail' | 'stats' | 'audit' }>()")
    expect(logs).toContain("() => props.viewMode || 'detail'")
    expect(logs).not.toContain('<a-radio-group')
  })
})
