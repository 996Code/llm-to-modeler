import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SchemaEditor from '../SchemaEditor.vue'

const { fetchKgKbs } = vi.hoisted(() => ({ fetchKgKbs: vi.fn() }))
vi.mock('../../../api', () => ({
  fetchKgKbs,
  induceKgSchema: vi.fn(),
  updateKgKb: vi.fn(),
}))
vi.mock('vue', async importOriginal => {
  const vue = await importOriginal<typeof import('vue')>()
  return { ...vue, onMounted: vue.onServerPrefetch }
})

beforeEach(() => vi.clearAllMocks())

describe('本体编辑器向量状态', () => {
  it.each([
    [true, 1024, '启用 dim=1024'],
    [true, null, '启用(维度待确定)'],
    [false, 1024, '未启用'],
  ])('显示当前状态 enabled=%s dim=%s', async (enabled, dim, label) => {
    fetchKgKbs.mockResolvedValue([{
      id: 'kb', vectorEnabled: enabled, vectorDim: dim,
      schema: { schema_mode: 'semi_open', entity_types: [], relation_types: [] },
    }])
    const app = createSSRApp(SchemaEditor, { kbId: 'kb' })
    app.provide('loadSafely', (fn: () => Promise<void>) => fn())
    for (const name of [
      'a-tag', 'a-radio-group', 'a-radio-button', 'a-button', 'a-collapse',
      'a-collapse-panel', 'a-alert', 'a-space', 'a-empty',
    ]) app.component(name, { template: '<div><slot /></div>' })
    const html = await renderToString(app)
    expect(html).toContain(label)
    expect(html).not.toContain('dim=null')
    expect(html).not.toContain('LLM_EMBED_MODEL')
  })
})
