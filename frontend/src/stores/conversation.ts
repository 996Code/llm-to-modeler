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
  const stageMessage = ref('')
  const currentStage = ref('')
  const pipelineSteps = ref<any[]>([])  // 动态 pipeline 步骤定义

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

  // 当前配置的格式化信息(从最近一条带 formattedData 的消息提取)
  const _latestFormattedData = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const msg = messages.value[i]
      if (msg.formattedData) return msg.formattedData
    }
    return null
  })

  const currentConfigFieldCount = computed(() => _latestFormattedData.value?.fieldCount)
  const currentConfigName = computed(() => _latestFormattedData.value?.formName || _latestFormattedData.value?.title)

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
      // 嵌入模式：保存会话 ID 到 localStorage，下次打开时恢复
      if (isEmbedded.value) {
        localStorage.setItem('embedded_conv_id', convId)
      }
      await loadConversations()
    }

    // Add user message
    messages.value.push({ role: 'user', content: text })
    streaming.value = true
    stageMessage.value = '正在理解您的意图...'
    currentStage.value = ''
    pipelineSteps.value = []  // 重置，等待后端 pipeline_definition 事件

    try {
      // 追问恢复:如果当前有 pendingClarification,把用户消息作为 answers 传给后端
      // 后端走 LangGraph Command(resume=answers) 从断点继续
      const clarifyAnswers = pendingClarification.value ? { text: text } : undefined

      await api.chat(text, convId || null, {
        onStage: (stage, msg) => {
          stageMessage.value = msg
          currentStage.value = stage
        },
        onPipelineDefinition: (tool, steps) => {
          // 接收后端发送的 pipeline 定义
          pipelineSteps.value = steps
        },
        onResult: (result) => {
          // onResult 分流顺序(由后端 ToolResult 三态决定):
          // 1. general  — 闲聊(reply 通道),显示纯文本
          // 2. needsClarification — 追问(ask 通道),显示问题卡片
          // 3. artifactType='data' — 数据结果(非配置),显示 data-card
          //    必须在 config 检查之前:artifactType 是后端设置的显式判别式
          // 4. result.config — 配置结果(默认),显示 config-card + 应用按钮
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
          // 数据结果 (artifact_type=data, 非配置类插件)
          // 必须在 config 检查之前:artifactType 是后端设置的显式判别式,
          // 而 result.config 只是隐式存在,优先级应低于显式判别式
          else if (result.artifactType === 'data') {
            const formattedData: Record<string, any> = {}
            if (result.fieldCount !== undefined) formattedData.fieldCount = result.fieldCount
            if (result.formName !== undefined) formattedData.formName = result.formName
            if (result.title !== undefined) formattedData.title = result.title
            messages.value.push({
              role: 'assistant',
              content: result.summary,
              dataResult: result.data || {},
              formattedData: Object.keys(formattedData).length > 0 ? formattedData : undefined,
            })
          }
          // 配置生成/修改成功 (artifact_type=config, 默认)
          else if (result.config) {
            currentConfig.value = result.config
            // 提取 formatted 字段(由后端 tool.format_result() 钩子提供)
            const formattedData: Record<string, any> = {}
            if (result.fieldCount !== undefined) formattedData.fieldCount = result.fieldCount
            if (result.formName !== undefined) formattedData.formName = result.formName
            if (result.formCode !== undefined) formattedData.formCode = result.formCode
            if (result.title !== undefined) formattedData.title = result.title
            if (result.valid !== undefined) formattedData.valid = result.valid
            messages.value.push({
              role: 'assistant',
              content: result.summary,
              configSnapshot: result.config,
              formattedData: Object.keys(formattedData).length > 0 ? formattedData : undefined,
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
      }, clarifyAnswers)
    } catch (e: any) {
      messages.value.push({ role: 'assistant', content: `请求失败: ${e.message}` })
    } finally {
      streaming.value = false
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
    stageMessage,
    currentStage,
    pipelineSteps,
    pendingClarification,
    isEmbedded,
    currentConfigFieldCount,
    currentConfigName,
    loadConversations,
    selectConversation,
    startNewConversation,
    removeConversation,
    sendMessage,
  }
})
