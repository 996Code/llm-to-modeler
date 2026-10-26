import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { cardOnlyResult, getResultRenderer } from '../../resultRegistry'

const artifact = () => ({
  pack: 'knowledge_graph', resultDisplay: 'card', type: 'kg_search_result',
  answer: '## Complete answer\n\nOriginal explanation [片段:1]',
  kb: { id: 'kb1', name: 'Knowledge' },
  subgraph: { nodes: [], edges: [] },
  sources: { entities: [], chunks: [{ kbId: 'kb1', docId: 'd1', docName: 'source.pdf', text: 'Evidence' }] },
  cited_chunks: [1],
})
const message = (dataResult: unknown = artifact()) => ({ role: 'assistant', content: 'Duplicate summary', dataResult })

describe('knowledge graph card routing', () => {
  it('opts in complete KG answers through the platform registry', () => {
    expect(getResultRenderer(message())).toBeDefined()
    expect(cardOnlyResult(message())).toBe(true)
  })
  it('accepts a complete no-evidence answer with empty graph and sources', () => {
    expect(cardOnlyResult(message({ ...artifact(), answer: 'No evidence found.', sources: { chunks: [] } }))).toBe(true)
  })
  it.each([undefined, null, '', '  ', {}, [], 42])('preserves summary for invalid answer %j', (answer) => {
    expect(cardOnlyResult(message({ ...artifact(), answer }))).toBe(false)
    expect(getResultRenderer(message({ ...artifact(), answer }))).toBeUndefined()
  })
  it.each([
    { pack: undefined }, { resultDisplay: undefined }, { resultDisplay: 'unknown' },
    { pack: 'other' }, { type: 'graph_result' }, { subgraph: null },
    { subgraph: { nodes: 'invalid', edges: [] } },
    { subgraph: { nodes: [null], edges: [] } },
    { subgraph: { nodes: [{ name: 'Missing id' }], edges: [] } },
    { subgraph: { nodes: [], edges: [{ source: 'Missing target' }] } },
    { sources: { entities: [], chunks: [null] } },
    { sources: { chunks: [{ text: {} }] } },
    { sources: { entities: {}, chunks: [] } },
  ])('leaves legacy and unsupported artifacts on the fallback path %j', (patch) => {
    expect(getResultRenderer(message({ ...artifact(), ...patch }))).toBeUndefined()
    expect(cardOnlyResult(message({ ...artifact(), ...patch }))).toBe(false)
  })
  it.each([{ role: 'user' }, { kind: 'error-diag' }, { needsClarification: true }])('does not hide error, Ask or user text %j', (patch) => {
    expect(cardOnlyResult({ ...message(), ...patch })).toBe(false)
  })
  it('does not claim historical artifacts without opt-in fields', () => {
    expect(getResultRenderer(message({ type: 'kg_search_result', kb: { name: 'Old' } }))).toBeUndefined()
  })
})

describe('knowledge graph result component', () => {
  it.each([false, true])('renders full answer once using supplied safe renderer; compact=%j', async (compact) => {
    const data = artifact()
    const component = getResultRenderer(message(data))
    expect(component).toBeDefined()
    const rendered: string[] = []
    const html = await renderToString(createSSRApp({ render: () => h(component!, {
      artifact: data, summary: 'Duplicate summary', labels: {}, compact, citedChunks: [1],
      renderMarkdown: (text: string) => { rendered.push(text); return '<p>Sanitized complete answer</p>' },
    }) }).component('a-tag', { render: () => h('span') }))
    expect(rendered).toEqual([data.answer])
    expect(html).not.toContain('Duplicate summary')
    expect(html.match(/Sanitized complete answer/g)).toHaveLength(1)
    expect(html).toContain('source.pdf')
    expect(html).toContain('下载来源文件')
    expect(html.includes('data-chunk-anchor="1"')).toBe(!compact)
  })
})
