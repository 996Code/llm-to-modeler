import { describe, expect, it } from 'vitest'
import {
  simpleItems, simpleLabel, simpleStats, isDevOnly,
  type TraceItem,
} from './turnTraceBusiness'

// 样例取自线上真实一轮(强检判定:用户消息→checkpoint/初始化/路由→LLM×N→检索→结论→执行工具)
const items: TraceItem[] = [
  { type: 'event', kind: 'user', at: 't1', payload: { message: '压力表需要强制检定吗' } },
  { type: 'event', kind: 'checkpoint', at: 't1b' }, // checkpoint 事件(线上实录)
  { type: 'event', kind: 'trace', at: 't2', payload: { title: 'checkpoint', detail: { rev: 5 } } },
  { type: 'event', kind: 'trace', at: 't2b', payload: { detail: { raw: 1 } } }, // 无标题纯 detail 打点
  { type: 'event', kind: 'trace', at: 't3', payload: { title: '请求初始化参数', detail: { user_id: 'anonymous' } } },
  { type: 'event', kind: 'trace', at: 't4', payload: { title: '意图路由', detail: { pack: 'metrology_assistant' } } },
  { type: 'call', callType: 'llm', stage: 'conversation_title', durationMs: 1500, at: 't5' },
  { type: 'call', callType: 'llm', stage: 'assistant.select', durationMs: 1300, at: 't6' },
  { type: 'call', callType: 'llm', stage: 'assistant.extract', durationMs: 1600, at: 't7' },
  { type: 'event', kind: 'stage', at: 't8', payload: { message: '检索「强检目录」资料…' } },
  { type: 'call', callType: 'graph', stage: 'kg.find_entities', durationMs: 800, at: 't9' },
  { type: 'call', callType: 'graph', stage: 'kg.subgraph', durationMs: 178, at: 't9b' }, // 同类不同 stage→按标签合并 ×2
  { type: 'call', callType: 'llm', stage: 'kg.query_embed', durationMs: 2616, at: 't9c' },
  { type: 'call', callType: 'vector', stage: 'kg.vector_search', durationMs: 1200, at: 't10' },
  { type: 'event', kind: 'stage', at: 't11', payload: { message: '检索「强检目录」资料…' } }, // 重复
  { type: 'call', callType: 'llm', stage: 'assistant.conclude', durationMs: 4000, at: 't12' },
  { type: 'event', kind: 'trace', at: 't13', payload: { title: '执行工具 metrology_assistant', detail: { tool: 'metrology_assistant', summary: '属于强制检定' } } },
  { type: 'event', kind: 'assistant', at: 't14', payload: { dataResult: {} } }, // 制品快照
]

describe('本轮链路简明视图', () => {
  it('隐藏开发者细节:checkpoint/初始化/路由/标题生成/无标题打点/制品快照', () => {
    expect(items.filter(isDevOnly)).toHaveLength(7) // checkpoint事件+trace×4(标题checkpoint/无标题/初始化/路由)+标题生成llm+制品快照
    const out = simpleItems(items)
    const labels = out.map(simpleLabel)
    expect(labels).not.toContain('checkpoint')
    expect(labels).not.toContain('请求初始化参数')
    expect(labels).not.toContain('处理节点') // 无标题打点不进简明时间线
    expect(labels).not.toContain('AI 处理') // conversation_title 被隐藏,不落下未知兜底标签
    expect(out.some(i => i.kind === 'assistant')).toBe(false)
  })
  it('标签翻译成业务语言', () => {
    const out = simpleItems(items)
    const labels = out.map(simpleLabel)
    expect(labels).toContain('你的提问')
    expect(labels).toContain('AI 理解需求')
    expect(labels).toContain('AI 提取关键信息')
    expect(labels).toContain('知识图谱检索')
    expect(labels).toContain('向量检索')
    expect(labels).toContain('AI 生成结论')
    expect(labels).toContain('问题向量化')
    expect(labels).toContain('执行查询')
    expect(labels).toContain('检索「强检目录」资料…')
  })
  it('同名阶段消息去重(即使中间隔着检索调用)', () => {
    const stageRows = simpleItems(items).filter(i => i.kind === 'stage')
    expect(stageRows).toHaveLength(1)
  })
  it('执行查询副标签带工具摘要(标题带工具名也命中)', () => {
    const exec = simpleItems(items).find(i => simpleLabel(i) === '执行查询') as any
    expect(exec.payload.detail.summary).toBe('属于强制检定')
  })
  it('相邻同类调用合并:两次图谱检索一行 ×2,耗时累加', () => {
    const graphs = simpleItems(items).filter(i => i.callType === 'graph')
    expect(graphs).toHaveLength(1)
    expect(graphs[0].mergedCount).toBe(2)
    expect(graphs[0].durationMs).toBe(978)
  })
  it('汇总统计:AI 4 次(标题生成不算)/检索 3 次/外部 0 次', () => {
    expect(simpleStats(items)).toEqual({ ai: 4, retrieve: 3, upstream: 0 })
  })
})
