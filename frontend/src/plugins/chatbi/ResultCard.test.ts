import { describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, h, nextTick, ref } from 'vue'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import { biPresentation } from './presentation'
import { renderToString } from 'vue/server-renderer'
import { Tag } from 'ant-design-vue'
import { cardOnlyResult, getResultRenderer } from '../resultRegistry'

const kpi = { chart_type: 'kpi', series: [{ type: 'gauge', data: [{ value: 0, name: 'Orders' }] }] }
const artifact = (extra: Record<string, any> = {}) => ({
  pack: 'chatbi', resultDisplay: 'card', answer: 'Complete business explanation.',
  chart_option: kpi, columns: ['Orders'], rows_sample: [[0]], rowcount: 1,
  ...extra,
})
const props = (data = artifact(), extra: Record<string, any> = {}) => ({
  artifact: data, summary: 'Transient summary.', labels: {},
  renderMarkdown: (text: string) => `<p>${text.replaceAll('<', '&lt;')}</p>`,
  compact: false, citedChunks: [], ...extra,
})
async function resultComponent() {
  const component = getResultRenderer({ role: 'assistant', dataResult: artifact() })
  expect(component, 'BI query artifact must have a plugin renderer').toBeDefined()
  return component!
}
async function html(data = artifact(), extra: Record<string, any> = {}) {
  const component = await resultComponent()
  const app = createSSRApp({ render: () => h(component, props(data, extra)) })
  app.component('a-tag', Tag)
  return renderToString(app)
}

describe('BI plugin result acceptance', () => {
  it.each([
    kpi,
    { chart_type: 'table' },
    ...['bar', 'line', 'pie', 'scatter'].map(type => ({ series: [{ type, data: [1] }] })),
  ])('accepts a renderable query result %j', chart => {
    const message = { role: 'assistant', dataResult: artifact({ chart_option: chart }) }
    expect(getResultRenderer(message) !== undefined).toBe(true)
    expect(cardOnlyResult(message)).toBe(true)
  })
  it.each([null, {}, { series: [] }, { series: [{ type: 'bar', data: [] }] },
    { series: [{ type: 'unknown', data: [1] }] }, { chart_type: 'kpi' }])('keeps text for absent or unsupported chart %j', chart => {
    const message = { role: 'assistant', dataResult: artifact({ chart_option: chart }) }
    expect(getResultRenderer(message) !== undefined).toBe(false)
    expect(cardOnlyResult(message)).toBe(false)
  })
  it.each([{ columns: [] }, { rows_sample: [] }, { rows_sample: [null] }])('requires actual table data %j', fields => {
    const message = { role: 'assistant', dataResult: artifact({ chart_option: { chart_type: 'table' }, ...fields }) }
    expect(getResultRenderer(message) !== undefined).toBe(false)
    expect(cardOnlyResult(message)).toBe(false)
  })
  it.each([{ role: 'user' }, { kind: 'error-diag' }, { needsClarification: true }])('preserves error and clarification paths %j', fields => {
    expect(getResultRenderer({ role: 'assistant', dataResult: artifact(), ...fields }) !== undefined).toBe(false)
  })
  it('does not suppress legacy artifacts without pack or display declaration', () => {
    expect(cardOnlyResult({ role: 'assistant', dataResult: { chart_option: kpi } })).toBe(false)
    expect(cardOnlyResult({ role: 'assistant', dataResult: artifact({ resultDisplay: undefined }) })).toBe(false)
  })
})

describe('BI result component with real BiChartCard', () => {
  it('renders full persisted answer exactly once and preserves zero KPI', async () => {
    const result = await html()
    expect(result.split('Complete business explanation.')).toHaveLength(2)
    expect(result).not.toContain('Transient summary.')
    expect(result).toMatch(/class="kpi-value"[^>]*>0<\/div>/)
    expect(result).toContain('Orders')
  })
  it('restores row count, truncation, degradation, warnings and metrics from artifact', async () => {
    const result = await html(artifact({
      chart_option: { chart_type: 'table' }, columns: ['City'], rows_sample: [['Nanjing']],
      rowcount: 100, truncated: true, retrieval_degraded: true, chart_degraded: true,
      execute_duration_ms: 40, total_duration_ms: 120, heal_rounds: 1,
      metric_hits: [{ metric: 'revenue', display_name: 'Revenue' }], persist_warnings: ['Save failed'],
    }))
    for (const value of ['Nanjing', '共 100 行', '结果被截断', '检索降级', '图表规则推断',
      'SQL 0.0s', '总耗时 0.1s', '自愈 1 轮', 'Revenue', 'Save failed']) expect(result).toContain(value)
  })
  it('merges live details over history defaults without discarding defaults', async () => {
    const result = await html(artifact({ total_duration_ms: 120, persist_warnings: ['Keep warning'] }), {
      detail: { totalDurationMs: 2300, llmCallCount: 2, promptTokens: 50, completionTokens: 20 },
    })
    for (const value of ['总耗时 2.3s', 'LLM ×2', '70 tokens', 'Keep warning']) expect(result).toContain(value)
    expect(result).not.toContain('总耗时 0.1s')
  })
  it('falls back to summary without duplicating it for older supported artifacts', async () => {
    const result = await html(artifact({ answer: undefined }))
    expect(result.split('Transient summary.')).toHaveLength(2)
  })
  it('does not mount an empty chart when there is no renderable option', async () => {
    const result = await html(artifact({ chart_option: null }))
    expect(result).not.toContain('chart-el')
    expect(result).not.toContain('Complete business explanation.')
  })
  it('uses supplied safe Markdown rendering for the persisted answer', async () => {
    const result = await html(artifact({ answer: '<script>unsafe</script>' }))
    expect(result).toContain('&lt;script>unsafe&lt;/script>')
    expect(result).not.toContain('<script>')
  })
  it('forwards the child chart export capability through a mounted parent ref', async () => {
    type Node = { children: Node[]; parent?: Node; text?: string }
    const host = createRenderer<Node, Node>({
      createElement: () => ({ children: [] }), createText: text => ({ children: [], text }),
      createComment: text => ({ children: [], text }), setText: (node, text) => { node.text = text },
      setElementText: (node, text) => { node.text = text }, parentNode: node => node.parent || null,
      nextSibling: () => null, patchProp: () => {},
      insert: (child, parent) => { parent.children.push(child); child.parent = parent }, remove: () => {},
    })
    // Vitest loads SFCs in SSR mode; compile the same real files for a host renderer ref test.
    function clientComponent(source: string, dependencies: Record<string, any>) {
      const { descriptor } = parse(source)
      const script = compileScript(descriptor, { id: 'bi-ref-test', inlineTemplate: true })
      const compiled = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
      const exports: Record<string, any> = {}
      new Function('require', 'exports', compiled)((name: string) => dependencies[name], exports)
      return exports.default
    }
    const childComponent = clientComponent(readFileSync(new URL('../../components/chat/BiChartCard.vue', import.meta.url), 'utf8'), { vue: Vue })
    const component = clientComponent(readFileSync(new URL('./ResultCard.vue', import.meta.url), 'utf8'), {
      vue: Vue, '../../components/chat/BiChartCard.vue': { default: childComponent }, './presentation': { biPresentation },
    })
    const card = ref<any>()
    const app = host.createApp({ render: () => h(component, { ...props(), ref: card }) })
    app.component('a-tag', Tag)
    app.mount({ children: [] })
    await nextTick()
    expect(card.value.getChartInstance()).toBeNull()
    const child = card.value.$.subTree.children.find((vnode: any) => vnode.type === childComponent)
    const chartInstance = { getDataURL: () => 'data:image/png;base64,chart' }
    const getInstance = vi.spyOn(child.component.exposed, 'getChartInstance').mockReturnValue(chartInstance)
    expect(card.value.getChartInstance()).toBe(chartInstance)
    expect(getInstance).toHaveBeenCalledOnce()
    app.unmount()
  })
})
