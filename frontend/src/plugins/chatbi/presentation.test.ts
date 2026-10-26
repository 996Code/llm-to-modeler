import { describe, expect, it } from 'vitest'
import { biPresentation } from './presentation'

const base = { role: 'assistant', dataResult: { pack: 'chatbi', resultDisplay: 'card', answer: '查询完成', chart_option: { series: [{ type: 'bar', data: [1] }] } } }

describe('bi presentation durations', () => {
  it('restores the persisted artifact duration when no live detail is injected', () => {
    const view = biPresentation({ ...base, dataResult: { ...base.dataResult, total_duration_ms: 10170, execute_duration_ms: 45 } })
    expect(view?.detail.totalDurationMs).toBe(10170)
    expect(view?.detail.executeDurationMs).toBe(45)
  })
  it('keeps live details over history defaults for fresh turns', () => {
    const view = biPresentation({ ...base, dataResult: { ...base.dataResult, total_duration_ms: 100 } },
      { totalDurationMs: 2300 })
    expect(view?.detail.totalDurationMs).toBe(2300)
  })
})
