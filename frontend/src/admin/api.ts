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

// 任务中心实例:/api/tasks 不在 /api/admin 前缀下,但共用同一把口令与 401 语义
export const tasksApi = axios.create({
  baseURL: (import.meta.env.BASE_URL || '/') + 'api/tasks',
  headers: { 'Content-Type': 'application/json' },
})

tasksApi.interceptors.request.use((config) => {
  const token = getAdminToken()
  if (token) config.headers['X-Admin-Token'] = token
  return config
})

tasksApi.interceptors.response.use(
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

export interface AdminPack {
  name: string
  enabled: boolean
  description: string
  fallback: string
  artifactType: string
  services: string[]
  tools: string[]
  /** 依赖检测结果:ok / missing_dependency / probe_failed */
  dependency?: DependencyStatus
  /** 是否声明了 settings.schema.yaml(有"设置"入口) */
  hasSettings?: boolean
  /** 自定义管理页组件 key(manifest admin.page;空 = 无自定义页) */
  adminPage?: string
  /** 自定义管理页标题(manifest admin.title) */
  adminTitle?: string
}

export interface DependencyStatus {
  status: 'ok' | 'missing_dependency' | 'probe_failed'
  missing: string[]
  detail: string
  dependencies?: Record<string, {
    status: string
    missing?: string[]
    detail?: string
    optional?: boolean
  }>
}

// ── 插件设置(声明式配置页) ─────────────────────────────────

export interface SettingsField {
  key: string
  type: 'string' | 'int' | 'bool' | 'enum' | 'secret'
  label: string
  env?: string
  default?: unknown
  required?: boolean
  help?: string
  placeholder?: string
  min?: number
  max?: number
  options?: string[]
}

export interface SettingsSchema {
  version?: number
  groups: { key: string; title: string; fields: SettingsField[] }[]
}

export interface PackSettingsPayload {
  name: string
  schema: SettingsSchema
  /** secret 字段为掩码哨兵 "__SET__"(已配置但不回显) */
  values: Record<string, unknown>
  dependency?: DependencyStatus
}

export async function fetchPackSettings(name: string): Promise<PackSettingsPayload> {
  const { data } = await adminApi.get(`/packs/${name}/settings`)
  return data
}

/** 保存插件配置(部分更新:只提交要改的键;secret 留空 = 保持不变)。 */
export async function savePackSettings(
  name: string,
  values: Record<string, unknown>,
): Promise<PackSettingsPayload> {
  const { data } = await adminApi.put(`/packs/${name}/settings`, { values })
  return data
}

/** 重新检测插件依赖(清探针缓存 → 全量评估;满足且启用中则热加载)。 */
export async function recheckPack(name: string): Promise<{
  name: string
  dependency: DependencyStatus
  reloaded: boolean
  loaded?: string[]
}> {
  const { data } = await adminApi.post(`/packs/${name}/recheck`)
  return data
}

// ── 任务中心 ─────────────────────────────────────────────

export type TaskStatus =
  | 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export interface TaskItem {
  id: string
  taskType: string
  packName: string
  title: string
  status: TaskStatus
  progress: number
  progressMessage: string
  queueKey: string
  payload: unknown
  result: unknown
  error: string
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
}

export interface TaskLogItem {
  id: number
  taskId: string
  level: 'info' | 'warn' | 'error'
  message: string
  data: unknown
  createdAt: string
}

export interface TaskTypeItem {
  type: string
  packName: string
}

export async function fetchTasks(params: {
  limit: number
  offset: number
  status?: string
  type?: string
  pack?: string
}): Promise<Paged<TaskItem>> {
  const { data } = await tasksApi.get('', { params })
  return data
}

export async function fetchTaskTypes(): Promise<{ items: TaskTypeItem[] }> {
  const { data } = await tasksApi.get('/types')
  return data
}

export async function fetchTask(taskId: string): Promise<TaskItem> {
  const { data } = await tasksApi.get(`/${taskId}`)
  return data
}

export async function fetchTaskLogs(taskId: string, after = 0): Promise<{ items: TaskLogItem[]; lastId: number }> {
  const { data } = await tasksApi.get(`/${taskId}/logs`, { params: { after } })
  return data
}

export async function cancelTask(taskId: string): Promise<TaskItem> {
  const { data } = await tasksApi.post(`/${taskId}/cancel`)
  return data
}

/**
 * 任务实时事件流(SSE 消费器;与主应用 streamSSE 同一套解析逻辑,
 * 这里独立实现是因为管理端需要带 X-Admin-Token 头)。
 *
 * 返回 abort 函数;断流不抛错——调用方可用日志轮询降级补齐。
 */
export function streamTaskEvents(
  taskId: string,
  handlers: {
    onSnapshot?: (d: { task: TaskItem; logs: TaskLogItem[] }) => void
    onProgress?: (d: { taskId: string; progress: number; message: string }) => void
    onLog?: (d: TaskLogItem) => void
    onStatus?: (d: { taskId: string; status: TaskStatus; error?: string }) => void
  },
): () => void {
  const controller = new AbortController()
  const base = import.meta.env.BASE_URL || '/'
  fetch(`${base}api/tasks/${taskId}/events`, {
    headers: { 'X-Admin-Token': getAdminToken() || '' },
    signal: controller.signal,
  })
    .then(async (resp) => {
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          let event = 'message'
          const dataLines: string[] = []
          for (const line of frame.split('\n')) {
            if (line.startsWith('event: ')) event = line.slice(7).trim()
            else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
          }
          if (!dataLines.length) continue
          try {
            const data = JSON.parse(dataLines.join('\n'))
            if (event === 'snapshot') handlers.onSnapshot?.(data)
            else if (event === 'progress') handlers.onProgress?.(data)
            else if (event === 'log') handlers.onLog?.(data)
            else if (event === 'status') handlers.onStatus?.(data)
          } catch { /* 忽略无法解析的帧(如心跳注释) */ }
        }
      }
    })
    .catch(() => { /* 断流:调用方用轮询兜底 */ })
  return () => controller.abort()
}

// ── 知识图谱插件(/api/packs/knowledge_graph;管理端口令 + 用户级 search) ──

export interface KgEntityTypeDef {
  key: string; label: string; description: string; examples?: string[]; color?: string
}
export interface KgRelationTypeDef {
  key: string; label: string; description: string; domain?: string[]; range?: string[]; color?: string
}
export interface KgSchema {
  schema_mode?: 'strict' | 'semi_open'
  entity_types?: KgEntityTypeDef[]
  relation_types?: KgRelationTypeDef[]
  pending_types?: { kind: 'entity' | 'relation'; key: string; label: string }[]
  pending_schema_induction?: {
    entity_types: KgEntityTypeDef[]; relation_types: KgRelationTypeDef[]
    induced_at?: string; sample_count?: number
  }
}
export interface KgKnowledgeBase {
  id: string; name: string; description: string
  schema: KgSchema | null; schemaTemplate: string
  embeddingModel: string; vectorDim: number | null; vectorEnabled: boolean
  status: string; createdAt: string; updatedAt: string
  docCount?: number; entityTotal?: number; relationTotal?: number
  graph?: { entities: number; relations: number }
}
export interface KgDocument {
  id: string; kbId: string; filename: string; mimeType: string
  sizeBytes: number; contentHash: string
  importStatus: 'uploaded' | 'importing' | 'succeeded' | 'partial' | 'failed'
  chunkCount: number; entityCount: number; relationCount: number
  error: string; createdAt: string; updatedAt: string
}
export interface KgGraphNode {
  id: string; name: string; normalized?: string; type: string
  description: string; aliases: string[]; sourceDocs: string[]
  typeStatus?: string
}
export interface KgGraphEdge {
  id: string; source: string; target: string; type: string
  description: string; evidence: string; docId: string
}
export interface KgGraphData { nodes: KgGraphNode[]; edges: KgGraphEdge[] }

export interface KgTemplate {
  key: string; title: string; description: string
  entityCount: number; relationCount: number
}

export interface KgSearchResult {
  answer: string
  kb: { id: string; name: string }
  subgraph: KgGraphData
  chunks: { chunkId: string; docName?: string; score?: number; seq?: number; text: string }[]
  sources: { entities: string[]; chunks: { docName?: string; score?: number; seq?: number }[] }
}

// pack API axios 实例(与 admin 同一把口令;search 用户级也经它,缺省 anonymous)
export const kgApi = axios.create({
  baseURL: (import.meta.env.BASE_URL || '/') + 'api/packs/knowledge_graph',
  headers: { 'Content-Type': 'application/json' },
})
kgApi.interceptors.request.use((config) => {
  const token = getAdminToken()
  if (token) config.headers['X-Admin-Token'] = token
  return config
})
kgApi.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error?.response?.status === 401) throw new UnauthorizedError('管理口令无效或已过期')
    throw error
  },
)

export async function fetchKgKbs(): Promise<KgKnowledgeBase[]> {
  const { data } = await kgApi.get('/kbs')
  return data.items
}
export async function fetchKgTemplates(): Promise<KgTemplate[]> {
  const { data } = await kgApi.get('/kbs/templates')
  return data.items
}
export async function createKgKb(payload: { name: string; description?: string; template?: string }): Promise<KgKnowledgeBase> {
  const { data } = await kgApi.post('/kbs', payload)
  return data
}
export async function updateKgKb(id: string, payload: { name?: string; description?: string; schema?: KgSchema }): Promise<KgKnowledgeBase> {
  const { data } = await kgApi.put(`/kbs/${id}`, payload)
  return data
}
export async function deleteKgKb(id: string): Promise<{ success: boolean; cleanupErrors: string[] }> {
  const { data } = await kgApi.delete(`/kbs/${id}`)
  return data
}
export async function fetchKgStats(id: string): Promise<Record<string, unknown>> {
  const { data } = await kgApi.get(`/kbs/${id}/stats`)
  return data
}

export async function fetchKgDocuments(kbId: string): Promise<KgDocument[]> {
  const { data } = await kgApi.get(`/kbs/${kbId}/documents`)
  return data.items
}
export async function uploadKgDocuments(kbId: string, files: File[]): Promise<{ filename: string; ok: boolean; reason?: string; document?: KgDocument }[]> {
  const form = new FormData()
  files.forEach((f) => form.append('files', f))
  const { data } = await kgApi.post(`/kbs/${kbId}/documents`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data.items
}
export async function deleteKgDocument(kbId: string, docId: string): Promise<{ success: boolean }> {
  const { data } = await kgApi.delete(`/kbs/${kbId}/documents/${docId}`)
  return data
}
export async function importKgDocument(kbId: string, docId: string, force = false): Promise<TaskItem> {
  const { data } = await kgApi.post(`/kbs/${kbId}/documents/${docId}/import`, { force })
  return data
}
export async function importKgAll(kbId: string, force = false): Promise<{ tasks: TaskItem[]; skipped: { docId: string; reason: string }[] }> {
  const { data } = await kgApi.post(`/kbs/${kbId}/import`, { force })
  return data
}
export async function induceKgSchema(kbId: string, sampleChunks = 8): Promise<TaskItem> {
  const { data } = await kgApi.post(`/kbs/${kbId}/schema/induce`, { sample_chunks: sampleChunks })
  return data
}

export async function fetchKgGraph(kbId: string, params: { q?: string; types?: string }): Promise<KgGraphData> {
  const { data } = await kgApi.get(`/kbs/${kbId}/graph`, { params })
  return data
}
export async function expandKgNode(kbId: string, nodeId: string): Promise<KgGraphData> {
  const { data } = await kgApi.get(`/kbs/${kbId}/graph/expand`, { params: { node_id: nodeId } })
  return data
}

export async function kgSearch(query: string, kb?: string): Promise<KgSearchResult> {
  const { data } = await kgApi.post('/search', { query, kb })
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
