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
  it('未知形状:通用扁平化兜底(标量入行,数组计数)', () => {
    const secs = formatDetail(unknown)
    const flat = JSON.stringify(secs)
    expect(flat).toContain('/api/xxx')
    expect(flat).toContain('2 项')
  })
})
