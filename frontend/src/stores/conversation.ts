// =============================================================================
// 模块说明：全局会话状态 Store（Pinia）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个 @Service + @Singleton 的全局状态 Bean，
// 在整个应用中只有一份实例，所有组件共享读写同一份状态。
//
// Pinia 用法说明（setup 写法）：
//   defineStore('名字', () => { ... setup 函数 ... })
//   setup 函数里用 ref/reactive 声明状态、computed 声明派生值、function 声明动作，
//   最后 return 出去，外部组件即可访问。
// =============================================================================

// defineStore：定义一个 store（类比 Spring 定义一个 Bean）
import { defineStore } from 'pinia'
// ref：声明响应式基本值（类比 Java 的 ObservableValue / SimpleObjectProperty）
//       —— 修改 .value 会自动通知所有依赖它的视图刷新
// computed：声明懒计算的派生值（类似 Java getter，但带缓存；依赖不变时不重算）
import { ref, computed } from 'vue'
// 仅导入类型（编译期检查，运行时不打包）
import type { Conversation, FormConfig, Message } from '../types'
// 引入 API 层全部导出（* as api → api.xxx 调用），类比 Java 的 @Autowired private ApiService api
import * as api from '../services/api'
// HostPort：嵌入模式读取宿主上下文（最新配置 + 服务地址表），注入每次 chat 请求
import { getHostPort, CONTEXT_KEY_ARTIFACT } from '../composables/hostPort'

/**
 * 嵌入模式下组装发给后端的附加上下文（每次发消息前调用）。
 * 返回三态：
 *   - undefined：非嵌入模式（无宿主），请求不带 context/services；
 *   - null：嵌入模式但拉取宿主上下文失败（链路异常）——调用方必须拦截并报错，
 *     不能静默降级（否则 AI 基于空配置回答，用户只会看到"我没拿到表单"的误导）；
 *   - 对象：成功，context.artifact 为宿主最新画布。
 */
async function refreshEmbedContext() {
  const port = getHostPort()
  if (!port.connected) return undefined
  // 拉宿主最新画布；失败先重试一次（瞬时抖动），仍失败则判链路异常
  let hostCtx = await port.getContext()
  if (!hostCtx) {
    await new Promise((r) => setTimeout(r, 300))
    hostCtx = await port.getContext()
  }
  if (!hostCtx) return null // 链路异常：让调用方拦截
  const contextKey = localStorage.getItem('embedded_context_key') || undefined
  const services = (port as any).hostServices as Record<string, string> | undefined
  return {
    context: {
      [CONTEXT_KEY_ARTIFACT]: hostCtx.artifact ?? undefined,
      revision: hostCtx.revision ?? null,
      contextKey,
    },
    services: services || undefined,
  }
}

// 定义并导出名为 'conversation' 的 store 工厂。
// 注意：use 开头 + 返回组合式对象的写法称为 "Setup Store"（Pinia 推荐写法）。
export const useConversationStore = defineStore('conversation', () => {
  // ---------------- 响应式状态（State）----------------
  // 每一个 ref() 就是一个独立可变、可被追踪的状态单元。
  const conversations = ref<Conversation[]>([])        // 历史会话列表
  const currentConversation = ref<Conversation | null>(null)  // 当前选中的会话
  const messages = ref<Message[]>([])                  // 当前会话的消息列表
  const currentConfig = ref<FormConfig | null>(null)    // 当前最新生成的表单配置
  // diff 基线：AI 产出与之对比，渲染红删绿增变更视图（GitLab 风格）。
  // 语义 = 「画布上/上一版的配置」：嵌入模式取宿主 GET_CONTEXT 的 artifact、
  // 应用成功后取应用版；独立模式兜底为上一份 AI 产出。
  const baselineConfig = ref<FormConfig | null>(null)

  /** 设置 diff 基线（深拷贝断开引用，避免后续修改互相污染）。 */
  function setBaseline(cfg: FormConfig) {
    baselineConfig.value = JSON.parse(JSON.stringify(cfg))
  }
  const loading = ref(false)                            // 是否正在加载（列表/详情）
  const streaming = ref(false)                          // 是否正在流式生成中（SSE 进行中）
  const stageMessage = ref('')                          // 当前阶段的提示文案（如"正在生成..."）
  const currentStage = ref('')                          // 当前阶段标识（如 "generate"）
  const pipelineSteps = ref<any[]>([])                  // 动态 pipeline 步骤定义（由后端推送）

  // ---------------- 派生状态（Computed / getter）----------------

  // 最后一条消息是否是追问（用于输入框提示文案切换）
  // computed 类似 Java 的懒 getter：依赖（messages/streaming）变化时才重算
  const pendingClarification = computed(() => {
    // 流式进行中不显示追问态
    if (streaming.value) return false
    const last = messages.value[messages.value.length - 1]
    // 最后一条是 assistant 且带 needsClarification 标记 → 处于追问态
    return last?.role === 'assistant' && last?.needsClarification === true
  })

  // 是否为嵌入模式：URL 带 embed=true，或当前窗口被 iframe 嵌入（有父窗口）
  const isEmbedded = computed(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get('embed') === 'true' || window.parent !== window
  })

  // 当前配置的格式化信息(从最近一条带 formattedData 的消息提取)
  // 这是一个私有 computed（下划线前缀表示内部使用，未在 return 中暴露）
  const _latestFormattedData = computed(() => {
    // 从后往前找第一条带 formattedData 的消息（最新的那条）
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const msg = messages.value[i]
      if (msg.formattedData) return msg.formattedData
    }
    return null
  })

  // 暴露的派生值：字段数、表单名（供 JSON 面板等展示）
  const currentConfigFieldCount = computed(() => _latestFormattedData.value?.fieldCount)
  const currentConfigName = computed(() => _latestFormattedData.value?.formName || _latestFormattedData.value?.title)

  // 链路错误横幅（嵌入模式握手/上下文失败时置位，EmbeddedLayout 顶部展示）
  const hostLinkError = ref('')

  // ---------------- 动作（Actions，类比 Service 方法）----------------

  /**
   * 加载历史会话列表。
   * try/finally 保证无论成功失败都把 loading 置回 false。
   */
  async function loadConversations() {
    loading.value = true
    try {
      conversations.value = await api.listConversations()
    } finally {
      loading.value = false
    }
  }

  /**
   * 选中某个会话：拉取详情并切换当前会话上下文。
   * @param id 会话 ID
   */
  async function selectConversation(id: string) {
    loading.value = true
    try {
      const conv = await api.getConversation(id)
      currentConversation.value = conv
      messages.value = conv.messages || []
      currentConfig.value = conv.currentConfig || null
      baselineConfig.value = null  // 切会话：diff 基线随下一轮交互重建
    } finally {
      loading.value = false
    }
  }

  /**
   * 开始新会话：后端创建后清空当前上下文。
   */
  async function startNewConversation() {
    const conv = await api.createConversation()
    currentConversation.value = conv
    messages.value = []
    currentConfig.value = null
    baselineConfig.value = null
    // 刷新侧边栏列表（让新会话出现）
    await loadConversations()
  }

  /**
   * 恢复会话：按 (userId, contextKey) 找该绑定下最新的历史会话并加载。
   * 嵌入模式下重开侧栏不丢上下文（对话与快照链都在同一会话里）。
   * @param userId   宿主下发的用户标识
   * @param contextKey 宿主实体标识（designer 场景 = formCode）
   */
  async function resumeConversation(userId: string, contextKey: string) {
    try {
      const conv = await api.findLatestConversationByContext(userId, contextKey)
      if (conv) {
        currentConversation.value = conv
        messages.value = conv.messages || []
        currentConfig.value = conv.currentConfig || null
        return
      }
    } catch {
      // 后端未支持该接口 / 查询失败：降级为新会话（Fail-Closed）
    }
    await startNewConversation()
  }

  /**
   * 删除会话；若删的正是当前会话，则清空当前上下文。
   * @param id 会话 ID
   */
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
   * 统一发送消息——后端自动识别意图（create/modify/general/image）。
   * 前端不再区分 generate/modify，统一调 /api/chat。
   *
   * @param text         用户输入文本
   * @param imageBase64  可选图片 base64（用于图片识别场景）
   */
  async function sendMessage(text: string, imageBase64?: string) {
    // 空消息或正在流式生成中，直接忽略
    if ((!text.trim() && !imageBase64) || streaming.value) return

    // Auto-create conversation if none selected
    // 若没有当前会话，先创建一个（懒创建模式）
    let convId = currentConversation.value?.id
    if (!convId) {
      // 嵌入模式：优先用宿主 contextKey 创建（便于按 (userId, contextKey) 恢复）
      const contextKey = isEmbedded.value ? localStorage.getItem('embedded_context_key') || undefined : undefined
      const conv = await api.createConversation('', contextKey)
      convId = conv.id
      currentConversation.value = conv
      // 嵌入模式：保存会话 ID 到 localStorage，下次打开时恢复
      if (isEmbedded.value) {
        localStorage.setItem('embedded_conv_id', convId)
      }
      await loadConversations()
    }

    // 嵌入模式：发消息前向宿主拉最新画布（GET_CONTEXT），确保 AI 基于最新版修改。
    // 拉取失败 = 链路异常：拦截本次发送并明确报错（不静默降级，避免 AI 基于空配置误导）
    // 注：refreshEmbedContext 只有嵌入态才可能返回 null，此处判 null 即嵌入链路异常
    const embedCtx = isEmbedded.value ? await refreshEmbedContext() : undefined
    // 独立模式：把当前制品作为上下文传给后端（后端 req.context 优先于会话存储）。
    // 场景：回滚/版本切换后 currentConfig 已变，继续修改必须基于当前生效版本
    const localCtx = (!isEmbedded.value && currentConfig.value)
      ? { context: { [CONTEXT_KEY_ARTIFACT]: currentConfig.value } }
      : undefined
    // 宿主画布即 diff 基线（每次发消息前都拉最新，基线随之刷新）
    if (embedCtx && embedCtx.context && embedCtx.context[CONTEXT_KEY_ARTIFACT]) {
      setBaseline(embedCtx.context[CONTEXT_KEY_ARTIFACT] as FormConfig)
    }
    if (embedCtx === null) {
      hostLinkError.value = '宿主上下文获取失败：嵌入链路异常，消息未发送。请刷新页面后重试'
      messages.value.push({
        role: 'assistant',
        content: `⚠️ ${hostLinkError.value}`,
      })
      return
    }

    // Add user message
    // 先把用户消息塞进列表（乐观更新，UI 立即显示）
    messages.value.push({ role: 'user', content: text })
    streaming.value = true
    stageMessage.value = '正在理解您的意图...'
    currentStage.value = ''
    pipelineSteps.value = []  // 重置，等待后端 pipeline_definition 事件

    try {
      // 追问恢复:如果当前有 pendingClarification,把用户消息作为 answers 传给后端
      // 后端走 LangGraph Command(resume=answers) 从断点继续
      const clarifyAnswers = pendingClarification.value ? { text: text } : undefined

      // 调用 chat，注册各类 SSE 事件回调（回调内根据事件更新状态/UI）
      // 嵌入模式：附加上下文（宿主最新配置 + 服务地址表），后端据此覆盖会话旧配置
      await api.chat(text, convId || null, {
        // 阶段更新回调
        onStage: (stage, msg) => {
          stageMessage.value = msg
          currentStage.value = stage
        },
        // 后端下发 pipeline 定义回调
        onPipelineDefinition: (tool, steps) => {
          // 接收后端发送的 pipeline 定义
          pipelineSteps.value = steps
        },
        // 收到最终结果回调：这里按优先级分流（决定 UI 渲染哪种卡片）
        onResult: (result) => {
          // onResult 分流顺序(由后端 ToolResult 三态决定):
          // 1. general  — 闲聊(reply 通道),显示纯文本
          // 2. needsClarification — 追问(ask 通道),显示问题卡片
          // 3. artifactType='data' — 数据结果(非配置),显示 data-card
          //    必须在 config 检查之前:artifactType 是后端设置的显式判别式
          // 4. result.config — 配置结果(默认),显示 config-card + 应用按钮
          if (result.intent === 'general') {
            // 闲聊：只塞一条纯文本助手消息
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
            // 从结果中抽取格式化字段（如有），放到 formattedData 供 UI 展示
            const formattedData: Record<string, any> = {}
            if (result.fieldCount !== undefined) formattedData.fieldCount = result.fieldCount
            if (result.formName !== undefined) formattedData.formName = result.formName
            if (result.title !== undefined) formattedData.title = result.title
            messages.value.push({
              role: 'assistant',
              content: result.summary,
              dataResult: result.data || {},
              // 仅在确实有格式化字段时才挂上，避免空对象
              formattedData: Object.keys(formattedData).length > 0 ? formattedData : undefined,
            })
          }
          // 配置生成/修改成功 (artifact_type=config, 默认)
          else if (result.config) {
            // 独立模式兜底：无宿主基线时，上一份产出就是 diff 基线
            if (baselineConfig.value == null && currentConfig.value != null) {
              baselineConfig.value = JSON.parse(JSON.stringify(currentConfig.value))
            }
            // 更新当前配置（侧边 JSON 面板会同步刷新）
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
          // 收到结果即清空阶段提示
          stageMessage.value = ''
          currentStage.value = ''
          // 异步刷新侧边会话列表（标题可能更新）
          loadConversations()
        },
        // 出错回调：插入一条错误提示消息
        onError: (err) => {
          messages.value.push({ role: 'assistant', content: `错误: ${err}` })
          stageMessage.value = ''
          currentStage.value = ''
        },
        // 流结束回调
        onDone: () => {
          stageMessage.value = ''
          currentStage.value = ''
        },
      }, clarifyAnswers, imageBase64, embedCtx ?? localCtx)
    } catch (e: any) {  // e: any —— 捕获任意异常并当作 any 处理（才能访问 .message）
      messages.value.push({ role: 'assistant', content: `请求失败: ${e.message}` })
    } finally {
      // 无论成功失败，都结束流式状态
      streaming.value = false
      stageMessage.value = ''
      currentStage.value = ''
    }
  }

  // 返回对外暴露的状态/派生值/动作。
  // 注意：return 后这些 ref/computed 就成为 store 的公共 API，
  // 组件里用 store.xxx 即可访问（Pinia 会自动解包 ref，无需写 .value）。
  return {
    conversations,
    currentConversation,
    messages,
    currentConfig,
    baselineConfig,
    setBaseline,
    loading,
    streaming,
    stageMessage,
    currentStage,
    hostLinkError,
    pipelineSteps,
    pendingClarification,
    isEmbedded,
    currentConfigFieldCount,
    currentConfigName,
    loadConversations,
    selectConversation,
    startNewConversation,
    resumeConversation,
    removeConversation,
    sendMessage,
  }
})
