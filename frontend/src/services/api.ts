import axios from 'axios'
import type { Conversation, FormConfig, SSEResult } from '../types'
import { getForwardedHeaders } from '../composables/forwardHeaders'

const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

// Set user ID header from URL or localStorage
function getUserId(): string {
  const params = new URLSearchParams(window.location.search)
  return params.get('userId') || localStorage.getItem('userId') || 'anonymous'
}

api.interceptors.request.use((config) => {
  config.headers['X-User-Id'] = getUserId()
  // 透传父系统的 headers（如 Authorization、X-Tenant-Id）
  const forwarded = getForwardedHeaders()
  for (const [key, val] of Object.entries(forwarded)) {
    config.headers[key] = val
  }
  return config
})

// ── Conversations ──────────────────────────────────────────

export async function listConversations(): Promise<Conversation[]> {
  const { data } = await api.get('/conversations')
  return data
}

export async function createConversation(title = ''): Promise<Conversation> {
  const { data } = await api.post('/conversations', { title })
  return data
}

export async function getConversation(id: string): Promise<Conversation> {
  const { data } = await api.get(`/conversations/${id}`)
  return data
}

export async function deleteConversation(id: string): Promise<void> {
  await api.delete(`/conversations/${id}`)
}

// ── Config Generation (SSE) ────────────────────────────────

export interface SSECallbacks {
  onStage?: (stage: string, message: string) => void
  onResult?: (result: SSEResult) => void
  onError?: (error: string) => void
  onDone?: () => void
}

/**
 * Generate form config via SSE stream.
 * Uses fetch + ReadableStream (not EventSource, because we need POST).
 */
/**
 * Unified chat entry — backend classifies intent automatically.
 * Replaces both generateConfig and modifyConfig.
 */
export async function chat(
  message: string,
  conversationId: string | null,
  callbacks: SSECallbacks,
): Promise<void> {
  await streamSSE(
    '/api/config/chat',
    { message, conversation_id: conversationId },
    callbacks,
  )
}

export async function generateConfig(
  description: string,
  conversationId: string | null,
  callbacks: SSECallbacks,
): Promise<void> {
  await streamSSE(
    '/api/config/generate',
    { description, conversation_id: conversationId },
    callbacks,
  )
}

export async function modifyConfig(
  currentConfig: FormConfig,
  instruction: string,
  conversationId: string | null,
  callbacks: SSECallbacks,
): Promise<void> {
  await streamSSE(
    '/api/config/modify',
    {
      current_config: currentConfig,
      instruction,
      conversation_id: conversationId,
    },
    callbacks,
  )
}

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

  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })

  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // SSE events are separated by \n\n
    const events = buffer.split('\n\n')
    buffer = events.pop() || '' // keep incomplete chunk

    for (const eventStr of events) {
      if (!eventStr.trim()) continue
      const event = parseSSEEvent(eventStr)
      if (!event) continue

      switch (event.type) {
        case 'stage':
          callbacks.onStage?.(event.data.stage, event.data.message)
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

function parseSSEEvent(raw: string): { type: string; data: any } | null {
  let type = ''
  let dataStr = ''
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim()
    else if (line.startsWith('data:')) dataStr += line.slice(5).trim()
  }
  if (!type) return null
  try {
    return { type, data: JSON.parse(dataStr) }
  } catch {
    return { type, data: {} }
  }
}

// ── Validate ───────────────────────────────────────────────

export async function validateConfig(
  config: FormConfig,
): Promise<{ valid: boolean; errors: any[]; warnings: string[] }> {
  const { data } = await api.post('/config/validate', { config })
  return data
}
