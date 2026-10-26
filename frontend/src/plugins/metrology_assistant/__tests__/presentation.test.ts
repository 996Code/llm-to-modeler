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
    expect(view?.decision).toBe('mandatory')
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
  it('renders a refusal as a notice rather than a completed success', () => {
    const view = metrologyPresentation({ role: 'assistant', dataResult: { type: 'metrology_notice', status: 'forbidden', title: '无权查询', message: '请核对单号' } })
    expect(view).toMatchObject({ kind: 'notice', status: 'forbidden', answer: '请核对单号' })
  })
})
