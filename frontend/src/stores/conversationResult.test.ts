import { describe, expect, it } from 'vitest'
import type { Message } from '../types'
import { applyConversationResult } from './conversationResult'

function state() {
  return {
    messages: [] as Message[],
    currentConfig: null,
    baselineConfig: null,
  }
}

describe('applyConversationResult', () => {
  it('keeps the complete data artifact when a result is resumed', () => {
    const result = {
      artifactType: 'data' as const,
      summary: '检索完成',
      data: {
        pack: 'knowledge_graph',
        resultDisplay: 'card',
        type: 'kg_search_result',
        answer: '答案 [片段:1]',
        cited_chunks: [1],
        sources: { chunks: [{ kbId: 'kb1', docId: 'doc1', docName: 'source.pdf', text: '证据' }] },
      },
    }
    const target = state()

    applyConversationResult(result, target)

    expect(target.messages).toEqual([{
      role: 'assistant',
      content: '检索完成',
      dataResult: result.data,
      formattedData: undefined,
    }])
    expect(target.messages[0].dataResult?.cited_chunks).toEqual([1])
  })

  it('preserves error, clarification, and config result semantics', () => {
    const errorTarget = state()
    applyConversationResult({ error: true, summary: '执行失败', message: 'details' }, errorTarget)
    expect(errorTarget.messages[0]).toMatchObject({
      content: '⚠️ 执行失败',
      kind: 'error-diag',
    })

    const clarificationTarget = state()
    const questions = [{ question: '选择类型', header: 'type', options: [{ label: 'A', description: 'a' }] }]
    applyConversationResult({ needsClarification: true, summary: '请确认', questions }, clarificationTarget)
    expect(clarificationTarget.messages[0]).toMatchObject({
      content: '请确认',
      needsClarification: true,
      clarificationQuestions: questions,
    })

    const config = { formCode: 'demo' } as any
    const configTarget = state()
    applyConversationResult({ summary: '已生成', config, formCode: 'demo', valid: true }, configTarget)
    expect(configTarget.currentConfig).toBe(config)
    expect(configTarget.messages[0]).toMatchObject({
      content: '已生成',
      configSnapshot: config,
      formattedData: { formCode: 'demo', valid: true },
    })
  })
})
