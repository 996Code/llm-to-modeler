// =============================================================================
// metrology_assistant 管理页 API 客户端 —— 场景配置(P1 管理页两 tab 数据面)。
// -----------------------------------------------------------------------------
// 与 kgApi/chatbiApi 同模式:双 token 拦截器;revision 乐观锁由调用方
// 携带(409 冲突 → 页面提示刷新重试)。
// =============================================================================
import axios from 'axios'
import { getAdminToken, UnauthorizedError } from './api'

export interface MetrologyScenario {
  label: string
  intent: string
  required: {
    slot: string
    header: string
    question: string
    options?: { label: string; description?: string }[]
    subject_slot?: string
    subject_default?: string
    normalize?: string
  }
  action: {
    kind: 'kg' | 'chatbi'
    env?: string
    ds_name?: string
    tool?: string
    params?: Record<string, string>
    stage?: string
    error_noun?: string
    notices?: Record<string, { title: string; message: string; order?: string }>
    card?: string
  }
}

export interface MetrologyConfig {
  revision: number
  factory: boolean
  content: { scenarios: Record<string, MetrologyScenario> }
}

export interface MetrologyHistoryItem {
  revision: number
  updatedBy: string
  updatedAt: string
  comment: string
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
