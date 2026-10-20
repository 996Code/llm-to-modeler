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
import { classifyError } from '../utils/errorDiag'

/**
 * 嵌入模式下组装发给后端的附加上下文（每次发消息前调用）。
 * 返回三态：
 *   - undefined：非嵌入模式（无宿主），请求不带 context/services；
 *   - null：嵌入模式但拉取宿主上下文失败（链路异常）——调用方必须拦截并报错，
 *     不能静默降级（否则 AI 基于空配置回答，用户只会看到"我没拿到表单"的误导）；
 *   - 对象：成功，context.artifact 为宿主最新画布。
 */
// 导出供协议链路测试（hostPort.test.ts）直接驱动；运行时仅本 store 使用
export async function refreshEmbedContext() {
  const port = getHostPort()
  if (!port.connected) return undefined
  // 拉宿主最新画布；失败先重试一次（瞬时抖动），仍失败则判链路异常
  let hostCtx = await port.getContext()
  if (!hostCtx) {
    await new Promise((r) => setTimeout(r, 300))
    hostCtx = await port.getContext()
  }
  // embed 模式的 URL 身份通道:?roles=A,B → pack_params.knowledge_graph.
  // user_roles(ACL 判定输入);?userId 由 userIdentity 层消费(X-User-Id)。
  // 宿主 GET_CONTEXT 的 packParams 优先,URL 只补 knowledge_graph.user_roles
  // 一项——无宿主协议的轻嵌入(如 embed-test 测试页)也经此注入身份,
  // 生产宿主不受影响。
  const urlQuery = new URLSearchParams(window.location.search)
  const urlRoles = urlQuery.get('roles')?.trim()
  const urlIdentity = !!(urlRoles || urlQuery.get('userId'))
  const hostless = urlQuery.get('hostless') === '1'
  let packParams = hostCtx?.packParams
  if (urlRoles) {
    const roles = urlRoles.split(',').map(r => r.trim()).filter(Boolean)
    if (roles.length) {
      packParams = { ...(packParams || {}), knowledge_graph: { ...(packParams?.knowledge_graph || {}), user_roles: roles } }
    }
  }
  // 链路异常且无 URL 身份/hostless 声明:让调用方拦截
  if (!hostCtx && !urlIdentity && !hostless) return null
  if (!hostCtx) hostCtx = {} // 无宿主的轻嵌入(URL 身份或 hostless 声明):继续,只注入身份
  const contextKey = localStorage.getItem('embedded_context_key') || undefined
  const services = (port as any).hostServices as Record<string, string> | undefined
  // 宿主 INIT 声明的插件链路集（packs）原样沿用到 chat 请求——与 INIT
  // 同名同义：单插件宿主（表单/列表/流程设计器）后端直通（零 LLM）；
  // 多插件声明后端在子集内路由（候选收窄）
  const packs = port.packs
  return {
    context: {
      [CONTEXT_KEY_ARTIFACT]: hostCtx.artifact ?? undefined,
      revision: hostCtx.revision ?? null,
      contextKey,
    },
    services: services || undefined,
    packs: packs ?? undefined,
    pack_params: packParams,
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

  // 最近一条数据制品消息(右侧面板"数据"视图用:chatbi 图表/SQL/表格等)
  const latestDataMessage = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const msg = messages.value[i]
      if (msg.dataResult) return msg
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
    // 切会话即清流式 UI 态(进度条/阶段提示):原会话若仍在流式中,其
    // SSE 回调已被会话切换守卫拦住不串扰这里;streaming 标志留给原流
    // finally 收尾,但 UI 上不再显示原会话的进度
    stageMessage.value = ''
    currentStage.value = ''
    pipelineSteps.value = []
    try {
      const conv = await api.getConversation(id)
      // 双重校验:加载期间用户可能又切了别的会话(含切到新会话=null)——
      // 晚到的响应不得覆盖当前选择。只认"当前仍是本次要加载的会话或
      // 尚无选择"两种状态
      if (currentConversation.value?.id !== undefined
          && currentConversation.value?.id !== id) {
        // 已切走,丢弃本次加载(以当前选择为准)
        loading.value = false
        return
      }
      currentConversation.value = conv
      // 历史消息里的 dataArtifact(数据型制品快照,如 KG 检索子图)映射到
      // dataResult——前端渲染分支复用 SSE 实时路径的同一条(图谱卡/数据卡)
      messages.value = (conv.messages || []).map((m: any) => {
        const art = m.dataResult ?? m.dataArtifact
        return {
          ...m,
          dataResult: art ?? undefined,
          // chatbi 制品恢复: formattedData 不落库, 从 dataArtifact 同构合成
          // (与 tool.format_result 钩子同构) —— 否则刷新后图表卡/指标标签消失。
          // M4 字段(sql/datasource_id/chart_config)一并合成——历史消息的
          // "加到看板"按钮依赖它们兜底定位, 刷新页面后不再消失。
          formattedData: art?.chart_option
            ? {
                chart: art.chart_option,
                metricHits: art.metric_hits || [],
                rowcount: art.rowcount,
                datasourceName: art.datasource_name,
                truncated: art.truncated,
                chartDegraded: art.chart_degraded,
                persistWarnings: art.persist_warnings || [],
                sql: art.sql,
                datasourceId: art.datasource_id,
                tables: art.tables,
                chartConfig: art.chart_config,
                savedQueryId: art.saved_query_id,
              }
            : undefined,
        }
      })
      currentConfig.value = conv.currentConfig || null
      baselineConfig.value = null  // 切会话：diff 基线随下一轮交互重建
    } finally {
      loading.value = false
    }
  }

  /**
   * 开始新会话:只重置本地上下文,不调后端(懒创建)。
   * 会话在首条消息发出时由后端创建并经 result 事件回传 id——
   * 避免"点一下新建就落库一条 0 消息空会话"(管理端列表曾被这类
   * 空会话刷屏)。侧边栏也不刷新:未入库的会话本就不该出现在列表里。
   */
  function startNewConversation() {
    currentConversation.value = null
    messages.value = []
    currentConfig.value = null
    baselineConfig.value = null
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
   * @param structuredAnswers 追问卡片点选项的结构化回答 {header: label}——
   *                     与打字回传的 {text} 是同一 answers 契约;后端工具
   *                     按 header(问题分类)取结构化答案,兼容自由文本
   */
  async function sendMessage(text: string, imageBase64?: string, structuredAnswers?: Record<string, string>) {
    // 空消息或正在流式生成中，直接忽略
    if ((!text.trim() && !imageBase64) || streaming.value) return

    // 会话 ID:独立模式首聊不带 id(后端懒创建,经 result 事件回传);
    // 嵌入模式保留预创建——contextKey 绑定/侧栏重开恢复依赖建会话时带上绑定
    let convId = currentConversation.value?.id
    if (!convId && isEmbedded.value) {
      const contextKey = localStorage.getItem('embedded_context_key') || undefined
      const conv = await api.createConversation('', contextKey)
      convId = conv.id
      currentConversation.value = conv
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

    // 追问态快照:必须在 push 用户消息之前读——pendingClarification 检查
    // "最后一条是否 assistant+needsClarification",push 之后最后一条变成
    // user,快照永远 false(真实事故:追问回答的 answers 从未发出过,
    // 每轮都当新消息重新路由,追问死循环)
    const wasClarifying = pendingClarification.value
    const clarifyAnswers = wasClarifying
      ? { ...(structuredAnswers || {}), text }
      : undefined

    // Add user message
    // 先把用户消息塞进列表（乐观更新，UI 立即显示）
    messages.value.push({ role: 'user', content: text })
    streaming.value = true
    stageMessage.value = '正在理解您的意图...'
    currentStage.value = ''
    pipelineSteps.value = []  // 重置，等待后端 pipeline_definition 事件

    // 会话切换守卫(用户 10-15 报障:SSE 回调闭包不绑会话,切到会话 B 后
    // A 的流式结果仍往 messages push——B 的界面显示 A 的回答)。
    // 发起时快照会话 id,回调里校验:已切走则丢弃(结果已落库,切回
    // A 时历史加载可见,不丢数据只丢串扰)。
    // null 边界(审查抓出):切到"新会话"(currentConversation=null)同样算
    // 切走——只允许"当前会话仍是发起会话"这一种状态通过,其余全拦
    const originConvId = currentConversation.value?.id
    const isStale = () => currentConversation.value?.id !== originConvId

    try {

      // 调用 chat，注册各类 SSE 事件回调（回调内根据事件更新状态/UI）
      // 嵌入模式：附加上下文（宿主最新配置 + 服务地址表），后端据此覆盖会话旧配置
      await api.chat(text, convId || null, {
        // 阶段更新回调
        onStage: (stage, msg) => {
          if (isStale()) return
          stageMessage.value = msg
          currentStage.value = stage
        },
        // 后端下发 pipeline 定义回调
        onPipelineDefinition: (tool, steps) => {
          if (isStale()) return
          // 接收后端发送的 pipeline 定义
          pipelineSteps.value = steps
        },
        // 收到最终结果回调：这里按优先级分流（决定 UI 渲染哪种卡片）
        onResult: (result) => {
          // 懒创建采纳:首条消息不带 id 发出的会话在此拿到后端回传的新 id,
          // 采纳后后续轮次/刷新/恢复都基于它(见 startNewConversation 注释)。
          // 守卫前置(审查 null 边界):从会话 A 切到新会话后,A 的结果不得
          // 把 A 的 id 采纳成新会话的 id
          if (isStale()) return   // 已切走:结果已落库,不串扰新会话界面
          if (result.conversationId && !currentConversation.value) {
            currentConversation.value = { id: result.conversationId } as Conversation
            loadConversations()  // 刷侧边栏,让新会话出现(不 await,不阻塞渲染)
          }
          // onResult 分流顺序(由后端 ToolResult 三态决定):
          // 0. error — 执行失败/前置校验拦截(error_for_llm 通道),显示错误提示
          // 1. general  — 闲聊(reply 通道),显示纯文本
          // 2. needsClarification — 追问(ask 通道),显示问题卡片
          // 3. artifactType='data' — 数据结果(非配置),显示 data-card
          //    必须在 config 检查之前:artifactType 是后端设置的显式判别式
          // 4. result.config — 配置结果(默认),显示 config-card + 应用按钮
          if (result.error) {
            // 工具执行失败/被前置校验拦截:显示诊断卡(P2#15——失败可定位
            // 环节+可查链路+可重试,不再是一行裸文本),不静默——用户必须
            // 知道这轮为什么没有产出
            messages.value.push({
              role: 'assistant',
              content: `⚠️ ${result.summary || result.message || '执行失败'}`,
              kind: 'error-diag',
              errorCategory: classifyError(
                `${result.error || ''} ${result.summary || ''} ${result.message || ''}`),
            })
          }
          // 闲聊
          else if (result.intent === 'general') {
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
            // chatbi 图表卡(formatted 平铺: ECharts option + 命中指标 + 元信息)
            if (result.chart !== undefined) formattedData.chart = result.chart
            if (result.metricHits !== undefined) formattedData.metricHits = result.metricHits
            if (result.rowcount !== undefined) formattedData.rowcount = result.rowcount
            if (result.datasourceName !== undefined) formattedData.datasourceName = result.datasourceName
            // 对话内明细(对标原系统 step_durations/self_heal_rounds)
            if (result.totalDurationMs !== undefined) formattedData.totalDurationMs = result.totalDurationMs
            if (result.healRounds !== undefined) formattedData.healRounds = result.healRounds
            if (result.executeDurationMs !== undefined) formattedData.executeDurationMs = result.executeDurationMs
            if (result.chartDegraded !== undefined) formattedData.chartDegraded = result.chartDegraded
            if (result.persistWarnings?.length) formattedData.persistWarnings = result.persistWarnings
            if (result.retrievalDegraded !== undefined) formattedData.retrievalDegraded = result.retrievalDegraded
            if (result.truncated !== undefined) formattedData.truncated = result.truncated
            if (result.tables !== undefined) formattedData.tables = result.tables
            // M4: 保存查询定位(加到看板/导出 CSV 直接引用本条)
            if (result.savedQueryId !== undefined) formattedData.savedQueryId = result.savedQueryId
            if (result.sql !== undefined) formattedData.sql = result.sql
            if (result.datasourceId !== undefined) formattedData.datasourceId = result.datasourceId
            if (result.chartConfig !== undefined) formattedData.chartConfig = result.chartConfig
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
        // 出错回调：插入诊断卡消息(P2#15——失败可定位环节+可查链路+可重试,
        // 不再是一行裸文本;分类从错误文本关键词推断,兜底"未知环节")
        onError: (err) => {
          if (isStale()) return
          messages.value.push({
            role: 'assistant',
            content: `错误: ${err}`,
            kind: 'error-diag',
            errorCategory: classifyError(err),
          })
          stageMessage.value = ''
          currentStage.value = ''
        },
        // 流结束回调
        onDone: () => {
          if (isStale()) return
          stageMessage.value = ''
          currentStage.value = ''
        },
      }, clarifyAnswers, imageBase64, embedCtx ?? localCtx)
    } catch (e: any) {  // e: any —— 捕获任意异常并当作 any 处理（才能访问 .message）
      if (isStale()) return
      messages.value.push({
        role: 'assistant',
        content: `请求失败: ${e.message}`,
        kind: 'error-diag',
        errorCategory: classifyError(e.message || ''),
      })
    } finally {
      // 无论成功失败，都结束流式状态
      streaming.value = false
      stageMessage.value = ''
      currentStage.value = ''
    }
  }

  // D10 历史加载续答:挂起轮断点续跑(该轮 user 已落库,只补 assistant)。
  // 复用 sendMessage 的 SSE 渲染管线(阶段/结果/错误/结束四回调同款),
  // 区别:不 push 用户消息、不带追问答案。
  async function resumePendingAnswer() {
    const convId = currentConversation.value?.id
    if (!convId || streaming.value) return
    streaming.value = true
    stageMessage.value = '正在继续生成被打断的回答...'
    const originConvId = convId
    const isStale = () => currentConversation.value?.id !== originConvId
    try {
      await api.chatResume(convId, {
        onStage: (stage, msg) => {
          if (isStale()) return
          stageMessage.value = msg
          currentStage.value = stage
        },
        onPipelineDefinition: (tool, steps) => {
          if (isStale()) return
          pipelineSteps.value = steps
        },
        onResult: (result) => {
          if (isStale()) return
          messages.value.push({
            role: 'assistant',
            content: result.summary || (result as any).reply || '',
            dataResult: (result as any).kg ? result : undefined,
          })
          stageMessage.value = ''
          currentStage.value = ''
          // 续答完成:清挂起标记(会话末尾不再悬空)
          if (currentConversation.value) {
            currentConversation.value.pendingResume = false
          }
          loadConversations()
        },
        onError: (err) => {
          if (isStale()) return
          messages.value.push({
            role: 'assistant',
            content: `续答失败: ${err}`,
            kind: 'error-diag',
            errorCategory: classifyError(err || ''),
          })
          stageMessage.value = ''
          currentStage.value = ''
        },
        onDone: () => {
          if (isStale()) return
          stageMessage.value = ''
          currentStage.value = ''
        },
      })
    } catch (e: any) {
      if (isStale()) return
      messages.value.push({
        role: 'assistant',
        content: `续答请求失败: ${e.message}`,
        kind: 'error-diag',
        errorCategory: classifyError(e.message || ''),
      })
    } finally {
      streaming.value = false
      stageMessage.value = ''
      currentStage.value = ''
    }
  }

// P2#15 失败诊断:错误文本 → 失败环节分类(给用户"哪坏了"的第一答案)
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
    latestDataMessage,
    loadConversations,
    selectConversation,
    resumePendingAnswer,
    startNewConversation,
    removeConversation,
    sendMessage,
  }
})
