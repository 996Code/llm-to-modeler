// =============================================================================
// 模块说明：API 服务层（HTTP 调用 + SSE 流式解析）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个 @Service 的 API 客户端（Feign / WebClient / RestTemplate）。
// 集中封装与后端的通信：会话 CRUD、聊天（SSE 流式）、配置校验。
//
// 两种通信模式：
//   1. 普通 REST：用 axios（类似 Spring 的 RestTemplate）做请求/响应
//   2. SSE 流式：用浏览器原生 fetch + ReadableStream 手动解析 Server-Sent Events
//      （不能用 axios，因为它不支持流式读取 response body）
// =============================================================================

// axios：流行的 HTTP 客户端库（类似 Java 的 OkHttp / Apache HttpClient）
import axios from 'axios'
import { CONTEXT_KEY_ARTIFACT } from '../composables/hostPort'
// 仅作为类型使用（import type），编译后不会进入运行时包，类似 Java 的接口引用
import type { Conversation, SSEResult } from '../types'
// 引入透传 headers 工具（嵌入模式下携带父系统的鉴权信息）
import { getForwardedHeaders } from '../composables/forwardHeaders'
// 引入 userId（嵌入模式下只信宿主下发的值，见 hostPort.ts）
import { getUserId } from '../composables/userIdentity'

// 统一项目前缀下的 API 基路径：BASE_URL（vite base，'/ai-modeler/'）+ 'api'。
// 这样无论直连本服务（13080）还是经宿主/网关反代（同前缀透传），请求都落在
// 正确路径上；SSE 的 fetch 也用同一常量（见 streamSSE / chat）。
export const API_BASE = (import.meta.env.BASE_URL || '/') + 'api'

// 创建一个预配置的 axios 实例。
// API_BASE 作为 baseURL：所有请求自动带上前缀（如 get('/conversations') → /ai-modeler/api/conversations）。
// 类比 Java：RestTemplate + 配置好 baseUrl 的 RequestFactory。
const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
})

// 获取当前用户 ID（用于请求头 X-User-Id）。
// 优先级：嵌入模式下由 hostPort.init() 写入的 userId（宿主下发，防伪造）→
//         URL 参数 userId → localStorage → 独立模式 admin / 嵌入兜底 anonymous
function getRequestUserId(): string {
  const hostId = getUserId()
  if (hostId) return hostId
  // window.location.search 是 URL 中 ? 后面的查询串
  const params = new URLSearchParams(window.location.search)
  // 是否为嵌入模式：URL 带 embed=true，或当前窗口有父窗口（被 iframe 嵌入）
  const isEmbedded = params.get('embed') === 'true' || window.parent !== window
  if (isEmbedded) {
    // 降级：URL 参数 > localStorage > 匿名用户兜底
    return params.get('userId') || localStorage.getItem('userId') || 'anonymous'
  }
  // 独立模式兜底用户为 admin
  return params.get('userId') || localStorage.getItem('userId') || 'admin'
}

// 注册 axios 请求拦截器：每个请求发出前自动注入 X-User-Id 和透传 headers。
// 类比 Java：Spring 的 ClientHttpRequestInterceptor / Filter，统一改写请求。
api.interceptors.request.use((config) => {
  // 注入用户标识头（嵌入模式优先宿主下发值）
  config.headers['X-User-Id'] = getRequestUserId()
  // 透传父系统的 headers（如 Authorization、X-Tenant-Id 等）
  const forwarded = getForwardedHeaders()
  for (const [key, val] of Object.entries(forwarded)) {
    config.headers[key] = val
  }
  return config
})

// ── 会话（Conversation）相关 REST 接口 ──────────────────────────

/**
 * 获取历史会话列表。
 * @returns Promise<Conversation[]> —— 异步返回会话数组。
 *          await 语法相当于 Java 的 CompletableFuture.get()。
 */
export async function listConversations(): Promise<Conversation[]> {
  // 解构赋值：只取响应体里的 data 字段（axios 把 HTTP 响应包了一层 {data,status,...}）
  const { data } = await api.get('/conversations')
  return data
}

/**
 * 创建新会话。
 * @param title 会话标题，默认空串
 * @param contextKey 可选，嵌入模式宿主实体标识（如 formCode），用于会话绑定恢复
 */
export async function createConversation(title = '', contextKey?: string): Promise<Conversation> {
  const { data } = await api.post('/conversations', { title, context_key: contextKey || '' })
  return data
}

/**
 * 按 ID 获取单个会话详情（含消息列表）。
 * @param id 会话 ID
 */
export async function getConversation(id: string): Promise<Conversation> {
  const { data } = await api.get(`/conversations/${id}`)
  return data
}

/**
 * 按 ID 删除会话。
 * @param id 会话 ID
 */
export async function deleteConversation(id: string): Promise<void> {
  await api.delete(`/conversations/${id}`)
}

// ── Pack manifest（通用层消费的声明：diff 身份键 / 展示字段 / 服务依赖）────

/** pack manifest（/api/meta/packs 返回项；字段对前端是不透明声明） */
export interface PackManifest {
  name: string
  artifact: {
    type: string
    identity: Record<string, string>
    display: Record<string, string>
    /** 制品卡动作集（pack 声明）：view_json / apply / rewind。
     *  未声明时后端回退最小集 ['view_json'] */
    actions?: string[]
  }
  services: string[]
}

/** manifest 模块级缓存（启动后不变） */
let _packCache: PackManifest[] | null = null

/**
 * 获取已加载 pack 的 manifest 声明（diff 对齐键 / 展示名来自这里）。
 * 失败返回空数组 → diff 退化为整体对比（Fail-Closed 降级）。
 *
 * 嵌入宿主可在 iframe URL 上声明要用的 pack 子集（?packs=njmind_form,xxx），
 * 拼到请求里让后端按「宿主声明 ∩ 部署方 PACKS_ENABLED」过滤；未传则该参数
 * 不出现，后端走自身默认（env 白名单或全量）。
 */
export async function getPackManifests(): Promise<PackManifest[]> {
  if (_packCache) return _packCache
  try {
    const packs = new URLSearchParams(window.location.search).get('packs')
    const { data } = await api.get('/meta/packs', { params: packs ? { packs } : undefined })
    _packCache = Array.isArray(data) ? data : []
  } catch {
    _packCache = []
  }
  return _packCache
}

// ── 配置生成（SSE 流式）────────────────────────────────────────

/**
 * 按 (userId, contextKey) 查找最新会话（嵌入模式会话恢复）。
 * 后端未实现时返回 null（Fail-Closed），调用方降级为新会话。
 * @param userId 用户标识
 * @param contextKey 宿主实体标识（如 formCode）
 */
export async function findLatestConversationByContext(userId: string, contextKey: string): Promise<Conversation | null> {
  try {
    const { data } = await api.get('/conversations', {
      params: { contextKey, latest: 'true' },
    })
    // 后端可能返回单对象（最新会话）或数组
    if (Array.isArray(data)) return data[0] ?? null
    return data ?? null
  } catch {
    return null
  }
}

/**
 * SSE 回调集合（监听不同事件类型的钩子）。
 * 用 interface 定义，字段都是可选的（? 前缀，类似 Java Optional / @Nullable）。
 * 调用方只需实现关心的回调，其余保持 undefined 即可。
 */
export interface SSECallbacks {
  onStage?: (stage: string, message: string) => void              // 阶段进度更新
  onPipelineDefinition?: (tool: string, steps: any[]) => void     // 后端下发 pipeline 步骤定义
  onResult?: (result: SSEResult) => void                          // 收到最终结果
  onError?: (error: string) => void                               // 出错
  onDone?: () => void                                             // 流结束
}

/**
 * 统一聊天入口 —— 后端自动识别意图（create/modify/general/image 等）。
 * 支持图片上传（image_base64 字段，供后端 ImageFormTool 识别）。
 *
 * @param message        用户输入文本
 * @param conversationId 当前会话 ID（首次对话可传 null）
 * @param callbacks      SSE 各类事件的回调
 * @param answers        追问回答（对应后端 LangGraph 的 Command(resume=answers)，从断点恢复）
 * @param imageBase64    图片 base64（用于 ImageFormTool 识别）
 * @param options        嵌入模式附加上下文：
 *                       context.artifact 宿主最新配置（覆盖会话旧配置，防陈旧基线）；
 *                       context.revision / contextKey 宿主签发版本与实体标识；
 *                       services 宿主服务地址表（如 {njmind-modeler: origin+/codeBack}）
 */
export async function chat(
  message: string,
  conversationId: string | null,
  callbacks: SSECallbacks,
  answers?: Record<string, any>,  // 追问时透传用户回答
  imageBase64?: string,           // 图片识别时透传 base64
  options?: {
    context?: { [CONTEXT_KEY_ARTIFACT]: unknown; revision?: string | null; contextKey?: string }
    services?: Record<string, string>
  },
): Promise<void> {
  await streamSSE(
    `${API_BASE}/config/chat`,
    {
      message,
      conversation_id: conversationId,
      answers,          // 追问时透传用户回答
      image_base64: imageBase64,  // 图片识别时透传 base64
      context: options?.context ?? undefined,   // 嵌入模式宿主最新上下文
      services: options?.services ?? undefined, // 宿主服务地址表
    },
    callbacks,
  )
}

/**
 * SSE 流式读取的核心实现（私有，不对外导出）。
 *
 * 为什么不用 axios？因为 axios 不支持流式读取响应体；这里用原生 fetch + ReadableStream。
 *
 * SSE 协议简述：服务端用文本流推送事件，每个事件由若干行组成，事件之间用空行（\n\n）分隔。
 * 每行形如 "event: xxx" 或 "data: {...}"。
 *
 * @param url       请求地址
 * @param body      请求体对象
 * @param callbacks 各类事件回调
 */
async function streamSSE(
  url: string,
  body: Record<string, any>,
  callbacks: SSECallbacks,
): Promise<void> {
  // 合并用户 ID + 父系统透传的 headers
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-User-Id': getRequestUserId(),
    ...getForwardedHeaders(),
  }

  // 发起 POST 请求（fetch 是浏览器原生 API，类似 Java 的 HttpClient）
  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })

  // 响应不成功或没有 body（流），直接抛异常
  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`)
  }

  // 获取流的 reader，逐块读取（异步迭代模式）
  const reader = resp.body.getReader()
  // TextDecoder 把字节流解码为字符串
  const decoder = new TextDecoder()
  // 缓冲区：存放"尚未凑成完整事件"的半截文本（网络分片可能把一个事件切成两块）
  let buffer = ''

  // ── 空闲看门狗 ──
  // 服务端每 15s 发心跳（: ping），30s 兜底 keepalive——正常流「字节最长 30s 一动」。
  // 连续 60s 无任何字节 = 连接已死（代理挂起/后端重启未通知/网络中断），
  // 主动取消并报错解锁：否则 fetch 永远 pending，UI 卡在"正在…"且无法再发消息
  //（历史上后端重启杀掉的旧流就是这种僵尸——必须有人先松手）。
  const controller = new AbortController()
  let lastActivity = Date.now()
  const watchdog = setInterval(() => {
    if (Date.now() - lastActivity > 60_000) {
      controller.abort()
    }
  }, 5_000)

  try {
  // 经典的流读取循环：读到 done=true 表示流结束
  while (true) {
    // 竞速：reader.read() vs 看门狗 abort。任何一字节到达都刷新活跃时间。
    const { done, value } = await Promise.race([
      reader.read(),
      new Promise<never>((_, reject) =>
        controller.signal.addEventListener('abort', () =>
          reject(new Error('SSE_IDLE_TIMEOUT')), { once: true })
      ),
    ])
    if (done) break
    lastActivity = Date.now()

    // 把新数据拼接到缓冲区。{ stream: true } 表示"可能还有后续"，避免多字节字符被截断
    buffer += decoder.decode(value, { stream: true })

    // SSE 事件之间用 \n\n 分隔，按此切成多个事件块
    const events = buffer.split('\n\n')
    // events.pop() 取出最后一个（很可能是不完整的块），留作下一轮继续拼接
    buffer = events.pop() || '' // keep incomplete chunk

    // 逐个解析并分发已完整的事件
    for (const eventStr of events) {
      if (!eventStr.trim()) continue
      const event = parseSSEEvent(eventStr)
      if (!event) continue

      // 根据事件类型分发到对应回调（switch 类似 Java 的 switch-case）
      switch (event.type) {
        case 'stage':
          callbacks.onStage?.(event.data.stage, event.data.message)
          break
        case 'pipeline_definition':
          callbacks.onPipelineDefinition?.(event.data.tool, event.data.steps)
          break
        case 'result':
          callbacks.onResult?.(event.data)
          break
        case 'error':
          callbacks.onError?.(event.data.error)
          break
        case 'done':
          callbacks.onDone?.()
          break
      }
    }
  }
  } catch (e: any) {
    if (String(e?.message).includes('SSE_IDLE_TIMEOUT')) {
      throw new Error('连接空闲超时（60 秒无响应）：链路已断开，请重试')
    }
    throw e
  } finally {
    clearInterval(watchdog)
    controller.abort() // 正常结束也调（no-op），确保监听器可回收
  }
}

/**
 * 解析单个 SSE 事件文本块为 { type, data } 结构。
 *
 * 输入形如：
 *   event: stage
 *   data: {"stage":"generate","message":"正在生成..."}
 *
 * @param raw 原始事件文本（不含分隔的 \n\n）
 * @returns 解析结果，或 null（没有 event 字段时无法识别）
 */
function parseSSEEvent(raw: string): { type: string; data: any } | null {
  let type = ''
  let dataStr = ''
  // 逐行扫描：行首为 "event:" 取类型，行首为 "data:" 累加数据
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim()
    else if (line.startsWith('data:')) dataStr += line.slice(5).trim()
  }
  if (!type) return null
  try {
    // 尝试把 data 解析为 JSON 对象
    return { type, data: JSON.parse(dataStr) }
  } catch {
    // JSON 解析失败时退回空对象，保证流程不中断
    return { type, data: {} }
  }
}
