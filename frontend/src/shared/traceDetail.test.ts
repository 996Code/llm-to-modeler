import { describe, expect, it } from 'vitest'
import { formatDetail } from './traceDetail'
import type { TraceCallLike } from './traceDetail'

// 样例取自本地真实 trace(强检判定轮)的 call_logs 数据形状
const llmSelect: TraceCallLike = {
  callType: 'llm', stage: 'assistant.select',
  requestData: {
    model: 'qwen3.7-plus',
    messages: [{ role: 'system', content: '你是…' }, { role: 'user', content: '选择一个能力,仅输出 JSON…' }],
  },
  responseData: {
    content: "{'scenario': 'mandatory_check'}",
    usage: { prompt_tokens: 924, completion_tokens: 132 },
  },
}
const findEntities: TraceCallLike = {
  callType: 'graph', stage: 'kg.find_entities',
  requestData: { stage: 'kg.find_entities', kb: '强检目录', terms: ['压力表'] },
  responseData: { hits: 1, termDetail: [{ term: '压力表', matched: '压力表', type: 'product' }] },
}
const subgraph: TraceCallLike = {
  callType: 'graph', stage: 'kg.subgraph',
  requestData: { seeds: ['压力表'], hops: 1 },
  responseData: {
    nodes: 7, edges: 6, nodesTruncated: false, edgesTruncated: false,
    nodeNames: ['压力表(product)', '强制检定(concept)'],
    triples: ['压力表 -[相关]-> 强制检定', '锅炉压力表 -[相关]-> 压力表'],
  },
}
const vector: TraceCallLike = {
  callType: 'vector', stage: 'kg.vector_search',
  requestData: { query: '压力表 贸易结算 强制检定', topK: 5 },
  responseData: {
    hits: 5, topScore: 0.7988, meanScore: 0.7411,
    results: [{ doc: '目录_演示版_.md', score: 0.7988, text: '用于贸易结算的压力表列入强制检定目录…' }],
  },
}
const unknown: TraceCallLike = {
  callType: 'upstream', stage: null,
  requestData: { url: '/api/xxx', method: 'GET', items: [1, 2] },
  responseData: { ok: true },
}

describe('链路明细格式化(共享格式化器)', () => {
  it('LLM:模型/指令摘要/输出文本/token', () => {
    const secs = formatDetail(llmSelect)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('qwen3.7-plus')
    expect(flat).toContain('选择一个能力')
    expect(flat).toContain("{'scenario': 'mandatory_check'}")
    expect(flat).toContain('924')
  })
  it('图谱实体查找:检索词+命中明细(词→实体(类型))', () => {
    const secs = formatDetail(findEntities)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('压力表')
    expect(flat).toContain('压力表 → 压力表（product）')
  })
  it('图谱子图:种子/子图规模+关系三元组列表', () => {
    const secs = formatDetail(subgraph)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('7 节点 · 6 关系')
    expect(flat).toContain('压力表 -[相关]-> 强制检定')
    expect(flat).not.toContain('已截断')
  })
  it('向量检索:检索词/命中数/相似度百分比+匹配片段', () => {
    const secs = formatDetail(vector)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('5 段')
    expect(flat).toContain('最高 80% · 平均 74%')
    expect(flat).toContain('目录_演示版_.md')
  })
  it('向量化:输入 N 条 M 字 → 输出 N 条 D 维(本地模型)', () => {
    const secs = formatDetail({
      callType: 'llm', stage: 'kg.query_embed',
      requestData: { stage: 'kg.query_embed', count: 1, chars: 18, backend: 'local', mode: 'query' },
      responseData: { count: 1, dim: 1024 },
    })
    const flat = JSON.stringify(secs)
    expect(flat).toContain('1 条 · 18 字 · 本地模型')
    expect(flat).toContain('1 条 · 1024 维向量')
  })
  it('空数据:兜底返回空数组,不输出废话占位', () => {
    expect(formatDetail({ callType: 'upstream', requestData: null, responseData: null })).toEqual([])
    expect(formatDetail({ callType: 'upstream', requestData: { a: '', b: null }, responseData: {} })).toEqual([])
  })
  it('事件类:payload.detail 有货才出行,无 detail 返回空', () => {
    const ev = formatDetail({ payload: { title: '执行工具 metrology_assistant',
      detail: { tool: 'metrology_assistant', summary: '属于强制检定' } } } as any)
    const flat = JSON.stringify(ev)
    expect(flat).toContain('属于强制检定')
    expect(formatDetail({ payload: { message: '你的提问' } } as any)).toEqual([])
    expect(formatDetail({} as any)).toEqual([])
  })
  it('未知形状:通用扁平化兜底(标量入行,数组计数)', () => {
    const secs = formatDetail(unknown)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('/api/xxx')
    expect(flat).toContain('2 项')
  })
})
