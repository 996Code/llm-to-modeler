import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as api from '../services/api'
import * as hostPort from '../composables/hostPort'
import type { Conversation } from '../types'
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
  vi.restoreAllMocks()
  vi.resetAllMocks()
  vi.mocked(api.listConversations).mockResolvedValue([])
  fakeWindow.location.search = ''
  vi.stubGlobal('window', fakeWindow)
  vi.stubGlobal('localStorage', {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  })
  setActivePinia(createPinia())
})

function gate<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

const hostContext: hostPort.HostContext = {
  artifact: { formCode: 'host' }, revision: 'r1',
  packParams: { njmind_form: { nodes: ['node-1'] }, web_search: { limit: 3 } },
}

function embeddedHost() {
  fakeWindow.location.search = '?embed=true'
  const port = new hostPort.PostMessageHostPort()
  port.hostPacks = ['njmind_form']
  port.hostServices = { modeler: 'https://host.example' }
  vi.spyOn(hostPort, 'getHostPort').mockReturnValue(port)
  const getContext = vi.spyOn(port, 'getContext').mockResolvedValue(hostContext)
  return { port, getContext }
}

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

describe('conversation request ownership', () => {
  for (const outcome of ['resolve', 'reject'] as const) {
    it(`keeps a new request locked when stale context ${outcome}s`, async () => {
      const { getContext } = embeddedHost()
      const oldContext = gate<hostPort.HostContext | null>()
      getContext.mockReturnValueOnce(oldContext.promise)
      const newEnd = gate()
      const newStarted = gate<api.SSECallbacks>()
      vi.mocked(api.chat).mockImplementation((_text, _id, callbacks) => {
        newStarted.resolve(callbacks)
        return newEnd.promise
      })
      const store = useConversationStore()
      store.currentConversation = { id: 'a', title: '' }
      const oldPending = store.sendMessage('old')
      store.startNewConversation()
      vi.mocked(api.createConversation).mockResolvedValue({ id: 'b', title: '' })
      const newPending = store.sendMessage('new')
      const callbacks = await newStarted.promise
      callbacks.onStage?.('new-stage', 'new progress')
      if (outcome === 'resolve') oldContext.resolve(hostContext)
      else oldContext.reject(new Error('old context failed'))
      await oldPending
      const snapshot = { streaming: store.streaming, stage: store.currentStage, messages: [...store.messages] }
      newEnd.resolve()
      await newPending
      expect(api.chat).toHaveBeenCalledTimes(1)
      expect(snapshot).toEqual({ streaming: true, stage: 'new-stage', messages: [{ role: 'user', content: 'new' }] })
      expect(store.currentConversation?.id).toBe('b')
    })
  }

  it('blocks chat and releases the lock when host context is unavailable', async () => {
    const { getContext } = embeddedHost()
    getContext.mockResolvedValue(null)
    const store = useConversationStore()
    store.currentConversation = { id: 'a', title: '' }
    await store.sendMessage('first')
    expect(store.hostLinkError).toContain('消息未发送')
    expect(store.messages).toHaveLength(1)
    expect(store.streaming).toBe(false)
    expect(store.stageMessage).toBe('')
    expect(api.chat).not.toHaveBeenCalled()
    getContext.mockResolvedValue(hostContext)
    await store.sendMessage('retry')
    expect(api.chat).toHaveBeenCalledTimes(1)
  })

  it('locks before create and ignores duplicate clicks while creation is pending', async () => {
    embeddedHost()
    const created = gate<Conversation>()
    vi.mocked(api.createConversation).mockReturnValue(created.promise)
    const store = useConversationStore()
    const first = store.sendMessage('first')
    const locked = store.streaming
    const duplicate = store.sendMessage('duplicate')
    created.resolve({ id: 'a', title: '' })
    await Promise.all([first, duplicate])
    expect(locked).toBe(true)
    expect(api.createConversation).toHaveBeenCalledTimes(1)
    expect(api.chat).toHaveBeenCalledTimes(1)
    expect(store.messages).toEqual([{ role: 'user', content: 'first' }])
  })

  it('discards a created conversation after the user opens a new window', async () => {
    const { getContext } = embeddedHost()
    const created = gate<Conversation>()
    vi.mocked(api.createConversation).mockReturnValue(created.promise)
    const store = useConversationStore()
    const pending = store.sendMessage('a')
    store.startNewConversation()
    created.resolve({ id: 'a', title: '' })
    await pending
    expect(store.currentConversation).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.baselineConfig).toBeNull()
    expect(store.streaming).toBe(false)
    expect(getContext).not.toHaveBeenCalled()
    expect(api.chat).not.toHaveBeenCalled()
  })

  it('rechecks ownership after the embedded conversation list refresh', async () => {
    const { getContext } = embeddedHost()
    const listed = gate<Conversation[]>()
    const listing = gate()
    vi.mocked(api.createConversation).mockResolvedValue({ id: 'a', title: '' })
    vi.mocked(api.listConversations).mockImplementation(() => {
      listing.resolve()
      return listed.promise
    })
    const store = useConversationStore()
    const pending = store.sendMessage('a')
    await listing.promise
    store.startNewConversation()
    listed.resolve([])
    await pending
    expect(store.currentConversation).toBeNull()
    expect(store.messages).toEqual([])
    expect(getContext).not.toHaveBeenCalled()
    expect(api.chat).not.toHaveBeenCalled()
  })

  it('does not send A or set its baseline after context waits across selecting B', async () => {
    const { getContext } = embeddedHost()
    const context = gate<hostPort.HostContext | null>()
    getContext.mockReturnValue(context.promise)
    vi.mocked(api.getConversation).mockResolvedValue({
      id: 'b', title: '', messages: [{ role: 'assistant', content: 'b history' }],
    })
    const store = useConversationStore()
    store.currentConversation = { id: 'a', title: '' }
    const pending = store.sendMessage('a question')
    await store.selectConversation('b')
    context.resolve(hostContext)
    await pending
    expect(store.currentConversation?.id).toBe('b')
    expect(store.messages).toEqual([{ role: 'assistant', content: 'b history' }])
    expect(store.baselineConfig).toBeNull()
    expect(api.chat).not.toHaveBeenCalled()
  })

  it('locks context waits and preserves host fields and pack restrictions', async () => {
    const { getContext } = embeddedHost()
    const context = gate<hostPort.HostContext | null>()
    getContext.mockReturnValue(context.promise)
    vi.mocked(localStorage.getItem).mockImplementation(key => key === 'embedded_context_key' ? 'canvas-1' : null)
    const store = useConversationStore()
    store.currentConversation = { id: 'a', title: '' }
    const first = store.sendMessage('first')
    const locked = store.streaming
    const duplicate = store.sendMessage('duplicate')
    context.resolve(hostContext)
    await Promise.all([first, duplicate])
    expect(locked).toBe(true)
    expect(api.chat).toHaveBeenCalledTimes(1)
    expect(store.messages).toEqual([{ role: 'user', content: 'first' }])
    expect(vi.mocked(api.chat).mock.calls[0]?.[5]).toEqual({
      context: { artifact: hostContext.artifact, revision: 'r1', contextKey: 'canvas-1' },
      services: { modeler: 'https://host.example' }, packs: ['njmind_form'],
      pack_params: { njmind_form: { nodes: ['node-1'] }, web_search: { limit: 3, enabled: false } },
    })
    expect(store.baselineConfig).toEqual(hostContext.artifact)
  })

  it('accepts callbacks after a first result adopts its conversation id', async () => {
    const ended = gate()
    vi.mocked(api.chat).mockReturnValue(ended.promise)
    const store = useConversationStore()
    const pending = store.sendMessage('first')
    const callbacks = vi.mocked(api.chat).mock.calls[0]![2]
    callbacks.onResult?.({ conversationId: 'created', intent: 'general', summary: 'first answer' })
    callbacks.onStage?.('finish', 'finishing')
    const stage = store.currentStage
    callbacks.onDone?.()
    const afterDone = store.stageMessage
    ended.resolve()
    await pending
    expect(store.currentConversation?.id).toBe('created')
    expect(stage).toBe('finish')
    expect(afterDone).toBe('')
    expect(store.messages.at(-1)?.content).toBe('first answer')
    expect(store.streaming).toBe(false)
  })

  for (const oldKind of ['send', 'resume'] as const) {
    for (const switchKind of ['new', 'select'] as const) {
      it(`isolates late ${oldKind} callbacks and finally from ${switchKind} window requests`, async () => {
        const oldEnd = gate()
        const newEnd = gate()
        const oldStarted = gate<api.SSECallbacks>()
        if (oldKind === 'send') {
          vi.mocked(api.chat).mockImplementationOnce((_text, _id, callbacks) => {
            oldStarted.resolve(callbacks)
            return oldEnd.promise
          })
        } else {
          vi.mocked(api.chatResume).mockImplementationOnce((_id, callbacks) => {
            oldStarted.resolve(callbacks)
            return oldEnd.promise
          })
        }
        const store = useConversationStore()
        store.currentConversation = { id: 'a', title: '', pendingResume: true }
        const oldPending = oldKind === 'send' ? store.sendMessage('old') : store.resumePendingAnswer()
        const oldCallbacks = await oldStarted.promise
        if (switchKind === 'new') store.startNewConversation()
        else {
          vi.mocked(api.getConversation).mockResolvedValue({ id: 'b', title: '', messages: [] })
          await store.selectConversation('b')
        }
        vi.mocked(api.chat).mockReturnValue(newEnd.promise)
        const newPending = store.sendMessage('new')
        const newCallbacks = vi.mocked(api.chat).mock.calls.at(-1)?.[2]
        newCallbacks?.onStage?.('new-stage', 'new progress')
        newCallbacks?.onPipelineDefinition?.('new-tool', [{ id: 'new-step' }])
        oldCallbacks.onStage?.('old-stage', 'old progress')
        oldCallbacks.onPipelineDefinition?.('old-tool', [{ id: 'old-step' }])
        oldCallbacks.onResult?.({ summary: 'old answer', config: { formCode: 'old' } as any })
        oldCallbacks.onError?.('old error')
        oldCallbacks.onDone?.()
        oldEnd.reject(new Error('late old failure'))
        await oldPending
        const whileNewPending = {
          streaming: store.streaming, stage: store.currentStage, message: store.stageMessage,
          steps: [...store.pipelineSteps], messages: [...store.messages], config: store.currentConfig,
        }
        newEnd.resolve()
        await newPending
        expect(whileNewPending).toEqual({
          streaming: true, stage: 'new-stage', message: 'new progress',
          steps: [{ id: 'new-step' }], messages: [{ role: 'user', content: 'new' }], config: null,
        })
        expect(store.streaming).toBe(false)
      })
    }
  }

  it('lets a new resume own its state after an old send finishes', async () => {
    const oldEnd = gate()
    const newEnd = gate()
    vi.mocked(api.chat).mockReturnValue(oldEnd.promise)
    vi.mocked(api.chatResume).mockReturnValue(newEnd.promise)
    vi.mocked(api.getConversation).mockResolvedValue({ id: 'b', title: '', pendingResume: true })
    const store = useConversationStore()
    store.currentConversation = { id: 'a', title: '' }
    const oldPending = store.sendMessage('old')
    await store.selectConversation('b')
    const resumed = store.resumePendingAnswer()
    await store.resumePendingAnswer()
    oldEnd.resolve()
    await oldPending
    const stillStreaming = store.streaming
    const callbacks = vi.mocked(api.chatResume).mock.calls[0]?.[1]
    callbacks?.onResult?.({ intent: 'general', summary: 'new resumed answer' })
    newEnd.resolve()
    await resumed
    expect(api.chatResume).toHaveBeenCalledTimes(1)
    expect(stillStreaming).toBe(true)
    expect(store.currentConversation?.pendingResume).toBe(false)
    expect(store.messages.at(-1)?.content).toBe('new resumed answer')
    expect(store.streaming).toBe(false)
  })

  for (const failure of ['create', 'list', 'context', 'chat', 'resume'] as const) {
    it(`cleans up an owned ${failure} rejection and allows retry`, async () => {
      const store = useConversationStore()
      if (failure === 'create') {
        embeddedHost()
        vi.mocked(api.createConversation).mockRejectedValueOnce(new Error('create failed'))
          .mockResolvedValue({ id: 'retry', title: '' })
      } else if (failure === 'list') {
        embeddedHost()
        vi.mocked(api.createConversation).mockResolvedValue({ id: 'a', title: '' })
        vi.mocked(api.listConversations).mockRejectedValueOnce(new Error('list failed'))
      } else if (failure === 'context') {
        const { getContext } = embeddedHost()
        getContext.mockRejectedValueOnce(new Error('context failed'))
        store.currentConversation = { id: 'a', title: '' }
      } else if (failure === 'chat') {
        vi.mocked(api.chat).mockRejectedValueOnce(new Error('chat failed'))
      } else {
        store.currentConversation = { id: 'a', title: '', pendingResume: true }
        vi.mocked(api.chatResume).mockRejectedValueOnce(new Error('resume failed'))
      }
      await (failure === 'resume' ? store.resumePendingAnswer() : store.sendMessage('first'))
      expect(store.streaming).toBe(false)
      expect(store.stageMessage).toBe('')
      expect(store.currentStage).toBe('')
      expect(store.messages.at(-1)).toMatchObject({ kind: 'error-diag', content: expect.stringContaining(`${failure} failed`) })
      await (failure === 'resume' ? store.resumePendingAnswer() : store.sendMessage('retry'))
      expect(store.streaming).toBe(false)
      if (failure === 'resume') expect(api.chatResume).toHaveBeenCalledTimes(2)
      else expect(api.chat).toHaveBeenCalledTimes(failure === 'chat' ? 2 : 1)
    })
  }
})

describe('selectConversation switching', () => {
  it('applies a second history load onto a different conversation', async () => {
    // 真实事故:载入一次历史后再载入另一段历史,提示成功但页面不变——
    // 旧守卫把"从已有会话切到别的会话"误判成"已切走"静默丢弃
    vi.mocked(api.getConversation).mockImplementation(async (id: string) => ({
      id, title: `t-${id}`,
      messages: [{ role: 'user', content: `msg-${id}` }],
    } as any))
    const store = useConversationStore()
    await store.selectConversation('a')
    expect(store.currentConversation?.id).toBe('a')
    expect(store.messages).toMatchObject([{ role: 'user', content: 'msg-a' }])
    await store.selectConversation('b')
    expect(store.currentConversation?.id).toBe('b')
    expect(store.messages).toMatchObject([{ role: 'user', content: 'msg-b' }])
  })

  it('discards the stale load when selections race', async () => {
    // 两次载入并发:先发的慢后回——晚到的旧响应不得覆盖新选择
    let releaseSlow!: () => void
    vi.mocked(api.getConversation).mockImplementation(async (id: string) => {
      if (id === 'slow') await new Promise<void>(r => { releaseSlow = r })
      return { id, title: '', messages: [{ role: 'user', content: id }] } as any
    })
    const store = useConversationStore()
    const slow = store.selectConversation('slow')
    await store.selectConversation('fast')
    releaseSlow()
    await slow
    expect(store.currentConversation?.id).toBe('fast')
    expect(store.messages).toMatchObject([{ role: 'user', content: 'fast' }])
  })

  it('invalidates an in-flight load when starting a new conversation', async () => {
    let release!: () => void
    vi.mocked(api.getConversation).mockImplementation(async () => {
      await new Promise<void>(r => { release = r })
      return { id: 'late', title: '', messages: [{ role: 'user', content: 'late' }] } as any
    })
    const store = useConversationStore()
    const pending = store.selectConversation('late')
    store.startNewConversation()
    release()
    await pending
    expect(store.currentConversation).toBeNull()
    expect(store.messages).toEqual([])
  })
})
