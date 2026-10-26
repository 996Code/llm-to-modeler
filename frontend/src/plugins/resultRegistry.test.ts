import { describe, expect, it } from 'vitest'
import { cardOnlyResult, getResultRenderer } from './resultRegistry'

describe('plugin result display contract', () => {
  const message = (dataResult: Record<string, unknown>) => ({ role: 'assistant', dataResult })
  const progress = { pack: 'metrology_assistant', type: 'metrology_progress', orderNo: 'BJ2026', status: '检测中' }
  it('only suppresses the summary when the plugin declares card and a renderer is available', () => {
    expect(cardOnlyResult(message(progress))).toBe(false)
    expect(cardOnlyResult(message({ ...progress, resultDisplay: 'card' }))).toBe(true)
    expect(getResultRenderer(message(progress))).toBeDefined()
  })
  it('preserves text when an unknown plugin has no suitable renderer', () => {
    expect(cardOnlyResult(message({ pack: 'unknown', type: 'unknown', resultDisplay: 'card' }))).toBe(false)
  })
  it('does not infer card-only from a plugin name or artifact type', () => {
    expect(cardOnlyResult(message({ pack: 'knowledge_graph', type: 'kg_search_result' }))).toBe(false)
    expect(cardOnlyResult(message({ pack: 'chatbi', type: 'chart' }))).toBe(false)
  })
  it('keeps errors and clarification on their existing rendering paths', () => {
    for (const extra of [{ kind: 'error-diag' }, { needsClarification: true }]) {
      expect(cardOnlyResult({ ...message({ ...progress, resultDisplay: 'card' }), ...extra })).toBe(false)
    }
  })
  it('does not hide text for unsupported or incomplete artifacts', () => {
    expect(cardOnlyResult(message({ pack: 'metrology_assistant', type: 'kg_search_result', resultDisplay: 'card' }))).toBe(false)
  })
})
