import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Conversation, FormConfig, Message } from '../types'
import * as api from '../services/api'

export const useConversationStore = defineStore('conversation', () => {
  const conversations = ref<Conversation[]>([])
  const currentConversation = ref<Conversation | null>(null)
  const messages = ref<Message[]>([])
  const currentConfig = ref<FormConfig | null>(null)
  const loading = ref(false)
  const streaming = ref(false)
  const streamingModify = ref(false)  // set dynamically from SSE stage events
  const stageMessage = ref('')
  const currentStage = ref('')

  // 最后一条消息是否是追问
  const pendingClarification = computed(() => {
    if (streaming.value) return false
    const last = messages.value[messages.value.length - 1]
    return last?.role === 'assistant' && last?.needsClarification === true
  })

  const isEmbedded = computed(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get('embed') === 'true' || window.parent !== window
  })

  async function loadConversations() {
    loading.value = true
    try {
      conversations.value = await api.listConversations()
    } finally {
      loading.value = false
    }
  }

  async function selectConversation(id: string) {
    loading.value = true
    try {
      const conv = await api.getConversation(id)
      currentConversation.value = conv
      messages.value = conv.messages || []
      currentConfig.value = conv.currentConfig || null
    } finally {
      loading.value = false
    }
  }

  async function startNewConversation() {
    const conv = await api.createConversation()
    currentConversation.value = conv
    messages.value = []
    currentConfig.value = null
    await loadConversations()
  }

  async function removeConversation(id: string) {
    await api.deleteConversation(id)
    if (currentConversation.value?.id === id) {
      currentConversation.value = null
      messages.value = []
      currentConfig.value = null
    }
    await loadConversations()
  }

  /**
   * 统一发送消息——后端自动识别意图（create/modify/general）。
   * 前端不再区分 generate/modify，统一调 /api/chat。
   */
  async function sendMessage(text: string) {
    if (!text.trim() || streaming.value) return

    // Auto-create conversation if none selected
    let convId = currentConversation.value?.id
    if (!convId) {
      const conv = await api.createConversation()
      convId = conv.id
      currentConversation.value = conv
      await loadConversations()
    }

    // Add user message
    messages.value.push({ role: 'user', content: text })
    streaming.value = true
    streamingModify.value = false  // 会被 SSE stage 事件动态覆盖
    stageMessage.value = '正在理解您的意图...'
    currentStage.value = ''

    try {
      await api.chat(text, convId || null, {
        onStage: (stage, msg) => {
          stageMessage.value = msg
          currentStage.value = stage
          // 根据 stage 动态判断管线类型
          // modify/generate_retry 关键词 → modify 管线
          if (stage.includes('modify')) {
            streamingModify.value = true
          }
        },
        onResult: (result) => {
          // 闲聊回复
          if (result.intent === 'general') {
            messages.value.push({
              role: 'assistant',
              content: result.summary,
            })
          }
          // 追问
          else if (result.needsClarification && result.questions) {
            messages.value.push({
              role: 'assistant',
              content: result.summary,
              needsClarification: true,
              clarificationQuestions: result.questions,
            })
          }
          // 配置生成/修改成功
          else if (result.config) {
            currentConfig.value = result.config
            messages.value.push({
              role: 'assistant',
              content: result.summary,
              configSnapshot: result.config,
            })
          }
          stageMessage.value = ''
          currentStage.value = ''
          loadConversations()
        },
        onError: (err) => {
          messages.value.push({ role: 'assistant', content: `错误: ${err}` })
          stageMessage.value = ''
          currentStage.value = ''
        },
        onDone: () => {
          stageMessage.value = ''
          currentStage.value = ''
        },
      })
    } catch (e: any) {
      messages.value.push({ role: 'assistant', content: `请求失败: ${e.message}` })
    } finally {
      streaming.value = false
      streamingModify.value = false
      stageMessage.value = ''
      currentStage.value = ''
    }
  }

  return {
    conversations,
    currentConversation,
    messages,
    currentConfig,
    loading,
    streaming,
    streamingModify,
    stageMessage,
    currentStage,
    pendingClarification,
    isEmbedded,
    loadConversations,
    selectConversation,
    startNewConversation,
    removeConversation,
    sendMessage,
  }
})
