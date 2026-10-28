import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as api from '../services/api'
import { useConversationStore } from './conversation'

vi.mock('../services/api', () => ({
  chat: vi.fn(),
  chatResume: vi.fn(),
  listConversations: vi.fn(async () => []),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
}))

const fakeWindow: any = {
  location: { search: '' },
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
}
fakeWindow.parent = fakeWindow

beforeEach(() => {
  vi.stubGlobal('window', fakeWindow)
  vi.stubGlobal('localStorage', {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  })
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('conversation store result consumption', () => {
  it('returns the current question token for typed answers', async () => {
    const store = useConversationStore()
    store.messages.push({ role: 'assistant', content: 'Choose', needsClarification: true,
      clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [], question_id: 'opaque / token:new' }],
    } as any)
    await store.sendMessage('free text')
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ text: 'free text', __question_id: 'opaque / token:new' })
  })

  it('returns the selected historical card token instead of the latest question token', async () => {
    const store = useConversationStore()
    store.messages.push({ role: 'assistant', content: 'Choose', needsClarification: true,
      clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [], question_id: 'new' }],
    } as any)
    await store.answerClarification({ question: 'Choose', header: 'kind', options: [], question_id: 'old' },
      { label: 'A', description: '' })
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ kind: 'A', text: 'A', __question_id: 'old' })
  })

  it('sends a historical token even when no clarification is currently pending', async () => {
    const store = useConversationStore()
    store.messages.push({ role: 'assistant', content: 'Done' })
    await store.answerClarification({ question: 'Choose', header: 'kind', options: [], question_id: 'old' },
      { label: 'A', description: '' })
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ kind: 'A', text: 'A', __question_id: 'old' })
  })

  it('does not borrow the current token for a legacy card with no token', async () => {
    const store = useConversationStore()
    store.messages.push({ role: 'assistant', content: 'Choose', needsClarification: true,
      clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [], question_id: 'new' }],
    } as any)
    await store.answerClarification({ question: 'Legacy', header: 'kind', options: [] },
      { label: 'A', description: '' })
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ kind: 'A', text: 'A' })
  })

  it('keeps other plugins typed answers token-free', async () => {
    const store = useConversationStore()
    store.messages.push({ role: 'assistant', content: 'Choose', needsClarification: true,
      clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [] }],
    })
    await store.sendMessage('free text')
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ text: 'free text' })
  })

  it('uses each reloaded historical question token without rebinding cards', async () => {
    vi.mocked(api.getConversation).mockResolvedValue({ id: 'history', title: '', messages: [
      { role: 'assistant', content: 'Old', needsClarification: true,
        clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [], question_id: 'old' }] },
      { role: 'assistant', content: 'New', needsClarification: true,
        clarificationQuestions: [{ question: 'Choose', header: 'kind', options: [], question_id: 'new' }] },
    ] } as any)
    const store = useConversationStore()
    await store.selectConversation('history')
    await store.answerClarification(store.messages[0]!.clarificationQuestions![0]!, { label: 'A', description: '' })
    expect(vi.mocked(api.chat).mock.calls[0]?.[3]).toEqual({ kind: 'A', text: 'A', __question_id: 'old' })
    expect(store.messages[1]!.clarificationQuestions![0]).toMatchObject({ question_id: 'new' })
  })
  it('uses the same data artifact contract for resume results and keeps citations', async () => {
    vi.mocked(api.chatResume).mockImplementation(async (_conversationId, callbacks) => {
      callbacks.onResult?.({
        artifactType: 'data',
        summary: '恢复完成',
        reply: '不应覆盖摘要',
        data: {
          pack: 'knowledge_graph',
          resultDisplay: 'card',
          type: 'kg_search_result',
          cited_chunks: [2],
          sources: { chunks: [{ kbId: 'kb1', docId: 'doc2', docName: 'resume.pdf', text: '证据' }] },
        },
      })
    })
    const store = useConversationStore()
    store.currentConversation = { id: 'conv-1', title: '', pendingResume: true }
    store.messages.push({ role: 'user', content: '继续' })

    await store.resumePendingAnswer()

    expect(store.messages.at(-1)).toMatchObject({
      role: 'assistant',
      content: '恢复完成',
      dataResult: expect.objectContaining({ cited_chunks: [2] }),
    })
    expect(store.currentConversation?.pendingResume).toBe(false)
  })

  it('keeps normal-send error, clarification, and config dirty semantics', async () => {
    const store = useConversationStore()
    const config = { formCode: 'new' } as any
    const oldConfig = { formCode: 'old' } as any
    store.currentConfig = oldConfig

    vi.mocked(api.chat)
      .mockImplementationOnce(async (_text, _id, callbacks) => {
        callbacks.onResult?.({ error: true, summary: '执行失败', message: 'details' })
      })
      .mockImplementationOnce(async (_text, _id, callbacks) => {
        const questions = [{ question: '选择', header: 'kind', options: [{ label: 'A', description: 'a' }] }]
        callbacks.onResult?.({ needsClarification: true, summary: '请确认', questions })
      })
      .mockImplementationOnce(async (_text, _id, callbacks) => {
        callbacks.onResult?.({ summary: '已生成', reply: '错误回退文本', config, formCode: 'new', valid: true })
      })

    await store.sendMessage('失败')
    await store.sendMessage('追问')
    await store.sendMessage('生成')

    expect(store.messages[1]).toMatchObject({ content: '⚠️ 执行失败', kind: 'error-diag' })
    expect(store.messages[3]).toMatchObject({
      content: '请确认',
      needsClarification: true,
      clarificationQuestions: expect.any(Array),
    })
    expect(store.currentConfig).toEqual(config)
    expect(store.baselineConfig).toEqual(oldConfig)
    expect(store.messages[5]).toMatchObject({
      content: '已生成',
      configSnapshot: config,
      formattedData: { formCode: 'new', valid: true },
    })
  })
})
