// =============================================================================
// metrology_assistant 管理页 API 客户端 —— 场景配置(P1 管理页两 tab 数据面)。
// -----------------------------------------------------------------------------
// 与 kgApi/chatbiApi 同模式:双 token 拦截器;revision 乐观锁由调用方
// 携带(409 冲突 → 页面提示刷新重试)。
// =============================================================================
import axios from 'axios'
import { getAdminToken, UnauthorizedError } from './api'

export interface MetrologyInputSchema {
  type?: string | string[]
  properties?: Record<string, MetrologyInputSchema>
  required?: string[]
  description?: string
  enum?: unknown[]
  [key: string]: unknown
}

export interface MetrologyScenario {
  [key: string]: unknown
  label: string
  intent: string
  _param_hints?: Record<string, string>
  required: {
    [key: string]: unknown
    slot?: string
    header?: string
    question?: string
    extract_hint?: string
    options?: { label: string; description?: string; [key: string]: unknown }[]
    subject_slot?: string
    subject_default?: string
    normalize?: string
    slot_type?: 'text' | 'enum' | 'number' | 'date'
    enum_options?: string[]
  }
  action: {
    [key: string]: unknown
    kind: 'kg' | 'chatbi'
    server?: string
    input_schema?: MetrologyInputSchema
    kb?: string
    kb_rules?: { slot: string; in: string[]; kb: string; [key: string]: unknown }[]
    query_template?: string
    answer_prompt?: string
    conclusion?: string
    datasource_id?: string
    env?: string
    ds_name?: string
    tool?: string
    params?: Record<string, string>
    stage?: string
    error_noun?: string
    notices?: Record<string, { title: string; message: string; order?: string; [key: string]: unknown }>
    card?: string
  }
}

export interface MetrologyConfig {
  revision: number
  factory: boolean
  content: { scenarios: Record<string, MetrologyScenario>; [key: string]: unknown }
}

export const metrologyApi = axios.create({
  baseURL: (import.meta.env.BASE_URL || '/') + 'api/packs/metrology_assistant',
  headers: { 'Content-Type': 'application/json' },
})
metrologyApi.interceptors.request.use((config) => {
  const token = getAdminToken()
  if (token) config.headers['X-Admin-Token'] = token
  const authToken = localStorage.getItem('auth_token')
  if (authToken) config.headers['Authorization'] = `Bearer ${authToken}`
  return config
})
metrologyApi.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error?.response?.status === 401) throw new UnauthorizedError('管理口令无效或已过期')
    throw error
  },
)
