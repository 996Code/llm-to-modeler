import { describe, expect, it } from 'vitest'
import { resultLabels } from './resultLabels'

describe('result labels are scoped to the owning plugin', () => {
  const displays = { knowledge_graph: { data_labels: { kb: '知识库', status: 'KG状态' } }, metrology_assistant: { data_labels: { orderNo: '委托单号', status: '状态' } } }
  it('uses the artifact owner rather than the first manifest', () => {
    expect(resultLabels({ pack: 'metrology_assistant' }, displays)).toEqual({ orderNo: '委托单号', status: '状态' })
  })
  it('does not borrow labels from another plugin for missing or unknown ownership', () => {
    expect(resultLabels({}, displays)).toEqual({})
    expect(resultLabels({ pack: 'unknown' }, displays)).toEqual({})
  })
})
