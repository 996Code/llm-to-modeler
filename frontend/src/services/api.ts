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
// 仅作为类型使用（import type），编译后不会进入运行时包，类似 Java 的接口引用
import type { Conversation, SSEResult } from '../types'
// 引入透传 headers 工具（嵌入模式下携带父系统的鉴权信息）
import { getForwardedHeaders } from '../composables/forwardHeaders'

// 创建一个预配置的 axios 实例。
// baseURL='/api' 表示所有请求会自动加上 /api 前缀（如 get('/conversations') → /api/conversations）。
// 类比 Java：RestTemplate + 配置好 baseUrl 的 RequestFactory。
const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

// 获取当前用户 ID（用于请求头 X-User-Id）。
// 独立页面默认 admin，嵌入页面从 URL 参数或 localStorage 获取
function getUserId(): string {
  // window.location.search 是 URL 中 ? 后面的查询串
  const params = new URLSearchParams(window.location.search)
  // 是否为嵌入模式：URL 带 embed=true，或当前窗口有父窗口（被 iframe 嵌入）
  const isEmbedded = params.get('embed') === 'true' || window.parent !== window
  if (isEmbedded) {
    // 优先级：URL 参数 > localStorage > 匿名用户兜底
    return params.get('userId') || localStorage.getItem('userId') || 'anonymous'
  }
  // 独立模式兜底用户为 admin
  return params.get('userId') || localStorage.getItem('userId') || 'admin'
}

// 注册 axios 请求拦截器：每个请求发出前自动注入 X-User-Id 和透传 headers。
// 类比 Java：Spring 的 ClientHttpRequestInterceptor / Filter，统一改写请求。
api.interceptors.request.use((config) => {
  // 注入用户标识头
  config.headers['X-User-Id'] = getUserId()
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
 */
export async function createConversation(title = ''): Promise<Conversation> {
  const { data } = await api.post('/conversations', { title })
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

// ── 配置生成（SSE 流式）────────────────────────────────────────

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
 */
export async function chat(
  message: string,
  conversationId: string | null,
  callbacks: SSECallbacks,
  answers?: Record<string, any>,  // 追问时透传用户回答
  imageBase64?: string,           // 图片识别时透传 base64
): Promise<void> {
  await streamSSE(
    '/api/config/chat',
    {
      message,
      conversation_id: conversationId,
      answers,          // 追问时透传用户回答
      image_base64: imageBase64,  // 图片识别时透传 base64
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
    'X-User-Id': getUserId(),
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

  // 经典的流读取循环：读到 done=true 表示流结束
  while (true) {
    const { done, value } = await reader.read()
    if (done) break

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

// ── 配置校验 ───────────────────────────────────────────────────

/**
 * 提交配置到后端做校验。
 * @param config 表单配置对象
 * @returns 校验结果：是否合法 + 错误列表 + 警告列表
 */
export async function validateConfig(
  config: Record<string, any>,
): Promise<{ valid: boolean; errors: any[]; warnings: string[] }> {
  const { data } = await api.post('/config/validate', { config })
  return data
}
