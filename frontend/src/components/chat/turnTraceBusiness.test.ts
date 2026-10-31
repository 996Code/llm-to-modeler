import { describe, expect, it } from 'vitest'
import {
  simpleItems, simpleLabel, simpleStats, isDevOnly,
  type TraceItem,
} from './turnTraceBusiness'

// 样例取自线上真实一轮(强检判定:用户消息→checkpoint/初始化/路由→LLM×3→执行工具→回复)
const items: TraceItem[] = [
  { type: 'event', kind: 'user', at: 't1', payload: { message: '压力表需要强制检定吗' } },
  { type: 'event', kind: 'trace', at: 't2', payload: { title: 'checkpoint', detail: { rev: 5 } } },
  { type: 'event', kind: 'trace', at: 't3', payload: { title: '请求初始化参数', detail: { user_id: 'anonymous' } } },
  { type: 'event', kind: 'trace', at: 't4', payload: { title: '意图路由', detail: { pack: 'metrology_assistant' } } },
  { type: 'call', callType: 'llm', stage: 'conversation_title', durationMs: 1500, at: 't5' },
  { type: 'call', callType: 'llm', stage: 'assistant.select', durationMs: 1300, at: 't6' },
  { type: 'call', callType: 'llm', stage: 'assistant.extract', durationMs: 1600, at: 't7' },
  { type: 'event', kind: 'stage', at: 't8', payload: { message: '检索「强检目录」资料…' } },
  { type: 'call', callType: 'graph', durationMs: 800, at: 't9' },
  { type: 'call', callType: 'vector', durationMs: 1200, at: 't10' },
  { type: 'event', kind: 'stage', at: 't11', payload: { message: '检索「强检目录」资料…' } }, // 连续重复
  { type: 'call', callType: 'llm', stage: 'assistant.conclude', durationMs: 4000, at: 't12' },
  { type: 'event', kind: 'trace', at: 't13', payload: { title: '执行工具', detail: { tool: 'metrology_assistant', summary: '属于强制检定' } } },
  { type: 'event', kind: 'assistant', at: 't14', payload: { dataResult: {} } }, // 制品快照
]

describe('本轮链路简明视图', () => {
  it('隐藏开发者细节:checkpoint/初始化/路由/会话标题生成/制品快照', () => {
    expect(items.filter(isDevOnly)).toHaveLength(4) // checkpoint+初始化+路由 3 条 trace + 标题生成 1 条 llm
    const out = simpleItems(items)
    const labels = out.map(simpleLabel)
    expect(labels).not.toContain('checkpoint')
    expect(labels).not.toContain('请求初始化参数')
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
    expect(labels).toContain('执行查询')
    expect(labels).toContain('检索「强检目录」资料…')
  })
  it('同名阶段消息去重(即使中间隔着检索调用)', () => {
    const stageRows = simpleItems(items).filter(i => i.kind === 'stage')
    expect(stageRows).toHaveLength(1)
  })
  it('执行查询副标签带工具摘要', () => {
    const exec = simpleItems(items).find(i => simpleLabel(i) === '执行查询') as any
    expect(exec.payload.detail.summary).toBe('属于强制检定')
  })
  it('汇总统计:AI 3 次(标题生成不算)/检索 2 次/外部 0 次', () => {
    expect(simpleStats(items)).toEqual({ ai: 3, retrieve: 2, upstream: 0 })
  })
})
