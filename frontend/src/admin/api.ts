// =============================================================================
// 管理端 API 层 —— /api/admin/* 的类型化客户端。
// -----------------------------------------------------------------------------
// 鉴权:所有请求自动携带 X-Admin-Token 头(localStorage 存放的口令)。
// 401 时抛 UnauthorizedError,由 AdminApp 捕获后回到口令输入页。
// 503(ADMIN_TOKEN 未配置)直接把后端 detail 透给 UI 提示运维。
// =============================================================================
import axios from 'axios'

// ── 类型(与 backend/src/api/admin.py 的返回结构对齐) ──────────────

export interface AdminStats {
  /** 鉴权模式:open=开放直连(无口令) / token=口令守门 */
  authMode?: 'open' | 'token'
  conversations: number
  users: number
  events: number
  messages: number
  traceEvents: number
  firstAt: string | null
  lastAt: string | null
  calls: {
    total: number
    llm: number
    llmMs: number
    upstream: number
    upstreamMs: number
    avgDurationMs: number | null
  }
  packs?: { discovered: number; enabled: number }
}

export interface AdminConversation {
  id: string
  userId: string
  contextKey: string
  title: string
  /** 展示标题:真实 title > 首条用户消息截断 > "新对话"(后端推导) */
  displayTitle?: string
  messageCount: number
  currentConfig: unknown
  createdAt: string
  updatedAt: string
}

export interface AdminConversationDetail extends AdminConversation {
  summary: string
  messages: { id: string; role: string; content: string; createdAt: string }[]
}

export interface Paged<T> {
  items: T[]
  total: number
  /** 会话列表端点回显分页参数;调用日志端点不回显(可选) */
  limit?: number
  offset?: number
}

export interface CallLogItem {
  id: string
  conv_id: string | null
  call_type: string
  endpoint: string
  request_data: unknown
  response_data: unknown
  status_code: number | null
  duration_ms: number | null
  error_message: string | null
  created_at: string
}

export interface AdminPack {
  name: string
  enabled: boolean
  description: string
  fallback: string
  artifactType: string
  services: string[]
  tools: string[]
}

// ── 链路追踪(trace)──────────────────────────────────────

/** 时间线项:事件(user/assistant/trace/checkpoint/...)或调用(llm/upstream) */
export interface TraceItem {
  at: string
  type: 'event' | 'call'
  /** event 专有:事件 kind */
  kind?: string
  payload?: {
    stage?: string
    title?: string
    status?: string
    duration_ms?: number | null
    detail?: unknown
    content?: string
    role?: string
    [key: string]: unknown
  }
  /** call 专有 */
  callType?: string
  endpoint?: string
  stage?: string | null
  statusCode?: number | null
  durationMs?: number | null
  errorMessage?: string | null
  requestData?: unknown
  responseData?: unknown
}

/** 一"轮"对话 = 一条用户消息到下一条之前的全部链路活动 */
export interface TraceTurn {
  index: number
  userContent: string | null
  startedAt: string
  endedAt: string
  wallMs: number
  llmCount: number
  llmMs: number
  upstreamCount: number
  upstreamMs: number
  items: TraceItem[]
}

export interface ConversationTrace {
  conversation: AdminConversationDetail
  summary: {
    turns: number
    events: number
    traceEvents: number
    llmCalls: number
    llmMs: number
    upstreamCalls: number
    upstreamMs: number
    firstAt: string | null
    lastAt: string | null
  }
  turns: TraceTurn[]
}

export interface PacksPayload {
  items: AdminPack[]
  stateFile: string
  source: string
}

// ── 口令管理 ──────────────────────────────────────────────

const TOKEN_KEY = 'admin_token'

export function getAdminToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setAdminToken(token: string) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// ── axios 实例 ────────────────────────────────────────────

export class UnauthorizedError extends Error {}

export const adminApi = axios.create({
  baseURL: (import.meta.env.BASE_URL || '/') + 'api/admin',
  headers: { 'Content-Type': 'application/json' },
})

adminApi.interceptors.request.use((config) => {
  const token = getAdminToken()
  if (token) config.headers['X-Admin-Token'] = token
  return config
})

adminApi.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error?.response?.status === 401) {
      throw new UnauthorizedError('管理口令无效或已过期')
    }
    throw error
  },
)

/** 从 axios 错误里取后端 detail(503 配置缺失等场景的可读提示)。 */
export function apiErrorMessage(error: unknown): string {
  const detail = (error as any)?.response?.data?.detail
  if (detail) return String(detail)
  return (error as Error)?.message || '请求失败'
}

// ── 接口封装 ──────────────────────────────────────────────

export async function fetchStats(): Promise<AdminStats> {
  const { data } = await adminApi.get<AdminStats>('/stats')
  return data
}

export async function fetchConversations(params: {
  limit: number
  offset: number
  userId?: string
  q?: string
}): Promise<Paged<AdminConversation>> {
  const { data } = await adminApi.get('/conversations', { params })
  return data
}

export async function fetchConversation(id: string): Promise<AdminConversationDetail> {
  const { data } = await adminApi.get(`/conversations/${id}`)
  return data
}

export async function fetchConversationTrace(id: string): Promise<ConversationTrace> {
  const { data } = await adminApi.get(`/conversations/${id}/trace`)
  return data
}

export async function deleteConversation(id: string): Promise<void> {
  await adminApi.delete(`/conversations/${id}`)
}

export async function fetchCallLogs(params: {
  limit: number
  offset: number
  convId?: string
  callType?: string
}): Promise<Paged<CallLogItem>> {
  const { data } = await adminApi.get('/call-logs', { params })
  return data
}

export async function fetchPacks(): Promise<PacksPayload> {
  const { data } = await adminApi.get('/packs')
  return data
}

export async function setPackEnabled(name: string, enabled: boolean): Promise<PacksPayload & { loaded?: string[] }> {
  const { data } = await adminApi.post(`/packs/${name}/${enabled ? 'enable' : 'disable'}`)
  return data
}

// ── 小工具 ────────────────────────────────────────────────

/** ISO 时间戳 → 本地可读时间(列表展示用)。 */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 会话/日志 ID 截断展示(全量太长,前 8 位足够人眼区分)。 */
export function shortId(id: string | null | undefined): string {
  if (!id) return '-'
  return id.length > 8 ? `${id.slice(0, 8)}…` : id
}

/** JSON 美化(请求/响应/明细展示共用);非 JSON 值退化为 String。 */
export function pretty(data: unknown): string {
  if (data == null) return '(空)'
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}
