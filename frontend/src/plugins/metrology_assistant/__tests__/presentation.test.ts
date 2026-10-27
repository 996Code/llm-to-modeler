import { describe, expect, it } from 'vitest'
import { metrologyPresentation } from '../presentation'

describe('metrology result presentation', () => {
  it('keeps plain replies, questions and errors unchanged', () => {
    for (const msg of [
      { role: 'assistant', content: '未找到委托单' },
      { role: 'assistant', needsClarification: true, dataResult: { type: 'metrology_progress' } },
      { role: 'assistant', kind: 'error-diag', dataResult: { type: 'metrology_progress' } },
      { role: 'user', dataResult: { type: 'metrology_progress' } },
    ]) expect(metrologyPresentation(msg)).toBeNull()
  })
  it('does not hide normal KG or other data replies', () => {
    expect(metrologyPresentation({ role: 'assistant', content: 'KG回答', dataResult: { type: 'kg_search_result' } })).toBeNull()
    expect(metrologyPresentation({ role: 'assistant', dataResult: { type: 'other' } })).toBeNull()
  })
  it('renders structured model output without parsing prose', () => {
    const answer = { decision: 'mandatory', conclusion: '属于强制检定', evidence: [{ text: '目录条款', chunk_refs: [1] }], suggestion: '按期送检' }
    const view = metrologyPresentation({ role: 'assistant', content: '重复摘要', dataResult: {
      pack: 'metrology_assistant', type: 'kg_search_result', answer, sources: { chunks: [{}] },
    } })
    expect(view?.decision).toBe('属于强制检定')
    expect(view?.answer).toContain('[片段:1]')
    expect(view?.answer).not.toContain('重复摘要')
  })
  it('restores old metrology cards using their stored summary', () => {
    expect(metrologyPresentation({ role: 'assistant', content: '旧结论 [片段:2]', dataResult: {
      type: 'kg_search_result', scenario: '强检判定',
    } })?.answer).toBe('旧结论 [片段:2]')
  })
  it('does not suppress text when no usable answer exists', () => {
    expect(metrologyPresentation({ role: 'assistant', dataResult: { type: 'kg_search_result', pack: 'metrology_assistant' } })).toBeNull()
  })
  it('includes all progress fields and query time with labels supplied by the plugin manifest', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: {
      type: 'metrology_progress', orderNo: 'BJ2027', instrument: '压力表', status: '已完成', progress: '5/5',
      currentStep: '发证', reportNo: 'RPT-1', finishedAt: '2026-10-22', validUntil: '2027-10-21', queriedAt: '2026-10-26 16:36', source: { secret: 'not displayed' },
    } }, { queriedAt: '查询时间', reportNo: '报告号' })
    expect(view?.fields).toContainEqual({ label: '查询时间', value: '2026-10-26 16:36' })
    expect(view?.fields).toContainEqual({ label: '报告号', value: 'RPT-1' })
    expect(view?.fields).toHaveLength(9)
    expect(JSON.stringify(view)).not.toContain('not displayed')
  })
  it('keeps invalid empty progress text', () => {
    expect(metrologyPresentation({ role: 'assistant', content: '保留提示', dataResult: { type: 'metrology_progress' } })).toBeNull()
  })
  it('retains a concrete no-evidence explanation inside a card-only answer', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: {
      type: 'kg_search_result', pack: 'metrology_assistant', answer: { decision: 'uncertain', conclusion: '无法确定', evidence: [], evidence_gap: '检定规程中未检索到扭矩扳手(内部使用)的相关依据', suggestion: '人工确认' },
    } })
    expect(view?.answer).toContain('检定规程中未检索到扭矩扳手(内部使用)的相关依据')
  })
  it('renders instrument records with unit formatting and keeps every row', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: {
      type: 'metrology_instrument', pack: 'metrology_assistant',
      instruments: [
        { name: '角膜曲率计', periodMonths: 12, feeYuan: 0, mandatoryL1: '验光仪器', mandatoryL2: '角膜曲率计' },
        { name: '无线温度记录器', periodMonths: 3, feeYuan: 150 },
      ],
    } }, { name: '器具名称', periodMonths: '检校周期', feeYuan: '检测费' })
    expect(view?.kind).toBe('instrument')
    expect(view?.records).toHaveLength(2)
    expect(view?.records?.[0]).toContainEqual({ label: '检校周期', value: '12 个月' })
    expect(view?.records?.[0]).toContainEqual({ label: '检测费', value: '0 元' })
    expect(view?.records?.[0]).toContainEqual({ label: '器具名称', value: '角膜曲率计' })
    expect(view?.records?.[1]).toContainEqual({ label: '检校周期', value: '3 个月' })
  })
  it('falls back to text when an instrument artifact has no usable rows', () => {
    expect(metrologyPresentation({ role: 'assistant', content: '保留摘要', dataResult: {
      type: 'metrology_instrument', pack: 'metrology_assistant', instruments: [{}, { model: '' }],
    } }, { name: '器具名称' })).toBeNull()
  })
  it('renders a refusal as a notice rather than a completed success', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: { type: 'metrology_notice', status: 'forbidden', title: '无权查询', message: '请核对单号' } })
    expect(view).toMatchObject({ kind: 'notice', status: 'forbidden', answer: '请核对单号' })
  })
  it('renders generic table artifacts (capability-import default card) as a table view', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: {
      type: 'metrology_table', pack: 'metrology_assistant', resultDisplay: 'card',
      columns: ['order_no', 'slot'], rows: [['BJ2026', '09:30'], ['BJ2027', '14:00']],
      rowTotal: 2, source: { datasource: '计量业务平台(通用)', tool: 'query_appointment' },
    } })
    expect(view?.kind).toBe('table')
    expect(view?.columns).toEqual(['order_no', 'slot'])
    expect(view?.rows).toEqual([['BJ2026', '09:30'], ['BJ2027', '14:00']])
    expect(view?.rowTotal).toBe(2)
    expect(JSON.stringify(view)).not.toContain('query_appointment')
  })
  it('caps generic table rows at 20 and reports the true total', () => {
    const rows = Array.from({ length: 30 }, (_, i) => [`BJ${i}`, '09:00'])
    const view = metrologyPresentation({ role: 'assistant', dataResult: {
      type: 'metrology_table', columns: ['order_no', 'slot'], rows, rowTotal: 30,
    } })
    expect(view?.rows).toHaveLength(20)
    expect(view?.rowTotal).toBe(30)
  })
  it('falls back to text when a table artifact has no columns', () => {
    expect(metrologyPresentation({ role: 'assistant', content: '保留摘要', dataResult: {
      type: 'metrology_table', columns: [], rows: [],
    } })).toBeNull()
  })
})

describe('场景变体知识卡(批次3)', () => {
  const base = (over: Record<string, any> = {}) => ({
    role: 'assistant', content: '摘要',
    dataResult: {
      type: 'kg_search_result', pack: 'metrology_assistant',
      scenario: '检测耗时/工作日咨询', answer_kind: 'turnaround',
      resultDisplay: 'card',
      answer: { decision: 'answered', conclusion: '约 5 个工作日',
                evidence: [{ text: '规程', chunk_refs: [1] }],
                suggestion: '以受理为准' },
      subgraph: { nodes: [], edges: [] }, sources: { chunks: [{}] },
      cited_chunks: [1], ...over,
    },
  })
  it('answered 决策渲染已答复卡,标题取场景名', () => {
    const v = metrologyPresentation(base(), {})
    expect(v?.kind).toBe('knowledge')
    expect(v?.title).toContain('检测耗时')
    expect(v?.decision).toBe('已答复')
    expect(v?.answer).toContain('约 5 个工作日')
  })
  it('qualification 三态标签映射', () => {
    const v = metrologyPresentation(base({
      scenario: '资质能力范围咨询(能否检测某器具)',
      answer: { decision: 'not_supported', conclusion: '不在范围',
                evidence: [{ text: 'x', chunk_refs: [1] }], suggestion: 's' },
    }), {})
    expect(v?.decision).toBe('不在承接范围')
  })
})
