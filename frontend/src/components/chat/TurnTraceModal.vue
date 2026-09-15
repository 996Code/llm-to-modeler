<template>
  <a-modal v-model:open="visible" :title="`本轮链路 · ${title}`" :footer="null" width="640px"
           destroy-on-close>
    <div v-if="loading" class="tc-loading"><a-spin tip="链路加载中..." /></div>
    <div v-else-if="!turn" class="tc-empty">暂无链路数据</div>
    <template v-else>
      <!-- 汇总条:耗时 / LLM 次数 / token -->
      <div class="tc-summary">
        <div class="tc-stat">
          <b>{{ fmtMs(turn.wallMs) }}</b><span>总耗时</span>
        </div>
        <div class="tc-stat">
          <b>{{ turn.llmCallCount ?? 0 }}</b><span>LLM 调用</span>
        </div>
        <div class="tc-stat">
          <b>{{ fmtTokens(turn.promptTokens) }}</b><span>输入 token</span>
        </div>
        <div class="tc-stat">
          <b>{{ fmtTokens(turn.completionTokens) }}</b><span>输出 token</span>
        </div>
      </div>

      <!-- 时间线:每行可点开看细节 -->
      <div class="tc-timeline">
        <div v-for="(item, i) in displayItems" :key="i"
             class="tc-item" :class="[itemClass(item), { open: expanded.has(i) }]">
          <div class="tc-item-head" @click="toggleExpand(i)">
            <span class="tc-dot" />
            <span class="tc-name">{{ itemLabel(item) }}</span>
            <span v-if="subLabel(item)" class="tc-sub-inline">{{ subLabel(item) }}</span>
            <span v-if="item.durationMs != null" class="tc-dur">{{ fmtMs(item.durationMs) }}</span>
            <DownOutlined class="tc-arrow" :class="{ open: expanded.has(i) }" />
          </div>
          <!-- 展开区:该阶段的全部细节 -->
          <div v-if="expanded.has(i)" class="tc-detail">
            <div class="tc-detail-meta">
              <span>时间 {{ fmtTime(item.at) }}</span>
              <span v-if="item.type === 'call' && item.statusCode != null">状态 {{ item.statusCode }}</span>
              <span v-if="item.type === 'call' && item.stage">阶段 {{ item.stage }}</span>
            </div>
            <div v-if="item.errorMessage" class="tc-error">{{ item.errorMessage }}</div>
            <div v-if="itemPayload(item)" class="tc-payload-block">
              <div class="tc-payload-head" @click.stop="toggleJsonRaw(i)">
                <span>细节 JSON</span>
                <span class="tc-payload-hint">{{ jsonRaw.has(i) ? '收起' : '格式化' }}</span>
              </div>
              <pre class="tc-payload">{{ itemPayloadText(item, i) }}</pre>
            </div>
          </div>
        </div>
      </div>
    </template>
  </a-modal>
</template>

<script setup lang="ts">
// 本轮链路弹窗(通用能力):查询完成后在对话内查看这一轮做了什么——
// 阶段时间线 + 每步耗时 + LLM 调用次数 + token 用量。
// 每行可点开:该阶段的时间/状态码/错误/完整 payload(细节 JSON)。
// 数据来自 GET /api/conversations/{id}/trace?turn=N(引擎级打点,任何 pack 可用)。
import { computed, ref, watch } from 'vue'
import { DownOutlined } from '@ant-design/icons-vue'
import { getConversationTrace } from '../../services/api'

// 链路条目/轮次类型(与服务层 getConversationTrace 对齐)
interface TraceItem {
  type: 'event' | 'call'
  kind?: string
  callType?: string
  stage?: string | null
  durationMs?: number | null
  statusCode?: number | null
  errorMessage?: string | null
  at: string
  payload?: Record<string, any>
  requestData?: Record<string, any> | null
  responseData?: Record<string, any> | null
  [k: string]: unknown
}
interface TraceTurnLocal {
  userContent?: string | null
  startedAt?: string
  endedAt?: string
  wallMs?: number
  llmCallCount?: number
  promptTokens?: number
  completionTokens?: number
  items?: TraceItem[]
}

const props = defineProps<{
  conversationId: string
  turnIndex: number          // 0-based 轮次
  title?: string             // 弹窗副标题(如用户问题截断)
}>()

const visible = defineModel<boolean>({ default: false })
const loading = ref(false)
const turn = ref<TraceTurnLocal | null>(null)
// 展开的行(细节区) + 该行 payload 以原始 JSON 展示(不格式化)
const expanded = ref(new Set<number>())
const jsonRaw = ref(new Set<number>())

watch(visible, async (v) => {
  if (!v || !props.conversationId) return
  loading.value = true
  turn.value = null
  expanded.value = new Set()
  jsonRaw.value = new Set()
  try {
    const data = await getConversationTrace(props.conversationId, props.turnIndex)
    turn.value = data.turns?.[0] || null
  } catch {
    turn.value = null
  } finally {
    loading.value = false
  }
})

function toggleExpand(i: number) {
  const s = new Set(expanded.value)
  if (s.has(i)) s.delete(i)
  else s.add(i)
  expanded.value = s
}
function toggleJsonRaw(i: number) {
  const s = new Set(jsonRaw.value)
  if (s.has(i)) s.delete(i)
  else s.add(i)
  jsonRaw.value = s
}

// 展示过滤:assistant 制品快照事件(纯数据回放)不进时间线, 只留过程性活动
const displayItems = computed(() =>
  (turn.value?.items || []).filter((i) => !(i.type === 'event' && i.kind === 'assistant')))

// 阶段名映射(引擎 + 常见 pack 打点;未知原样展示)
const _STAGE_LABELS: Record<string, string> = {
  route_pack: '意图路由', route_tool: '工具选择', compress_history: '历史压缩',
  'chatbi.embed': '问题向量化', 'chatbi.retrieve': '表结构检索',
  'chatbi.retrieve.refine': '检索精筛', 'chatbi.think': '预思考',
  'chatbi.generate_sql': 'SQL 生成', 'chatbi.execute': 'SQL 执行',
  'chatbi.check': '结果校验', 'chatbi.heal': 'SQL 自愈',
  'chatbi.chart': '图表生成', 'chatbi.memory.extract': '记忆提炼',
  'chatbi.memory.consolidate': '记忆整理',
}
const _KIND_LABELS: Record<string, string> = {
  user: '用户消息', assistant: '回复完成', stage: '阶段推进', trace: '链路打点',
}

function itemLabel(item: TraceItem): string {
  if (item.type === 'call') {
    if (item.callType === 'llm') return `LLM · ${_STAGE_LABELS[item.stage || ''] || item.stage || '调用'}`
    return `调用 · ${item.callType}`
  }
  const kind = item.kind || ''
  if (kind === 'trace') {
    const payload = (item as any).payload || {}
    return payload.title || payload.stage || '链路打点'
  }
  return _KIND_LABELS[kind] || kind
}

function subLabel(item: any): string {
  if (item.type === 'event' && item.kind === 'stage') {
    return (item.payload?.message) || ''
  }
  if (item.type === 'event' && item.kind === 'trace' && item.payload?.detail) {
    const d = item.payload.detail
    const parts: string[] = []
    for (const [k, v] of Object.entries(d).slice(0, 3)) {
      if (typeof v === 'string' || typeof v === 'number') parts.push(`${k}: ${v}`)
    }
    return parts.join(' · ')
  }
  if (item.type === 'call' && item.errorMessage) return item.errorMessage
  return ''
}

// 展开区细节:call 条目带请求/响应摘要, event 条目带 payload
function itemPayload(item: TraceItem): Record<string, any> | null {
  const it = item as any
  // call 条目:请求摘要(去掉超长字段)+ 响应摘要
  if (it.type === 'call') {
    const out: Record<string, any> = {}
    const req = it.requestData
    if (req && typeof req === 'object') {
      out.request = _shrink(req)
    }
    const resp = it.responseData
    if (resp && typeof resp === 'object') {
      out.response = _shrink(resp)
    }
    return Object.keys(out).length ? out : null
  }
  const p = it.payload
  if (p && typeof p === 'object' && Object.keys(p).length) return _shrink(p)
  return null
}

// 摘要化:超长字符串截断(细节弹窗不是全量数据查看器), 嵌套对象递归两层
function _shrink(v: any, depth = 0): any {
  if (typeof v === 'string') {
    return v.length > 500 ? `${v.slice(0, 500)}…(${v.length} 字符)` : v
  }
  if (Array.isArray(v)) {
    return v.length > 20 ? [...v.slice(0, 20).map((x) => _shrink(x, depth + 1)), `…共 ${v.length} 项`] : v.map((x) => _shrink(x, depth + 1))
  }
  if (v && typeof v === 'object' && depth < 3) {
    const out: Record<string, any> = {}
    for (const [k, val] of Object.entries(v)) out[k] = _shrink(val, depth + 1)
    return out
  }
  return v
}

function itemPayloadText(item: TraceItem, i: number): string {
  const p = itemPayload(item)
  if (!p) return ''
  return jsonRaw.value.has(i) ? JSON.stringify(p) : JSON.stringify(p, null, 2)
}

function itemClass(item: any): string {
  if (item.statusCode != null && item.statusCode >= 400) return 'err'
  if (item.type === 'event' && item.kind === 'user') return 'user'
  if (item.type === 'event' && item.kind === 'assistant') return 'done'
  return ''
}

function fmtMs(ms?: number | null): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
function fmtTokens(n?: number): string {
  if (!n) return '0'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
function fmtTime(at?: string): string {
  if (!at) return '-'
  const d = new Date(at)
  return isNaN(d.getTime()) ? at : d.toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<style scoped>
.tc-loading, .tc-empty { padding: 40px 0; text-align: center; color: var(--text-placeholder); }

/* 汇总条 */
.tc-summary { display: flex; gap: 10px; margin-bottom: 14px; }
.tc-stat {
  flex: 1; background: var(--bg-hover); border-radius: var(--radius-md);
  padding: 10px 0; text-align: center;
}
.tc-stat b { display: block; font-size: 16px; color: var(--text-primary); }
.tc-stat span { font-size: 11px; color: var(--text-secondary); }

/* 时间线 */
.tc-timeline { max-height: 420px; overflow-y: auto; }
.tc-item { padding: 6px 0 6px 4px; border-left: 2px solid var(--border-color-light); margin-left: 6px; padding-left: 14px; position: relative; }
.tc-dot {
  position: absolute; left: -5px; top: 12px; width: 8px; height: 8px;
  border-radius: 50%; background: var(--color-primary);
}
.tc-item-head { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.tc-item-head:hover .tc-name { color: var(--color-primary); }
.tc-name { font-size: 13px; color: var(--text-primary); font-weight: 500; flex-shrink: 0; }
.tc-sub-inline {
  font-size: 11.5px; color: var(--text-secondary); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0;
}
.tc-dur { font-size: 11px; color: var(--text-secondary); margin-left: auto; font-family: var(--font-mono); flex-shrink: 0; }
.tc-arrow { font-size: 10px; color: var(--text-placeholder); flex-shrink: 0; transition: transform 0.15s; }
.tc-arrow.open { transform: rotate(180deg); }

/* 展开的细节区 */
.tc-detail {
  margin-top: 6px; padding: 8px 10px; background: var(--bg-hover);
  border-radius: var(--radius-md); font-size: 12px;
}
.tc-detail-meta { display: flex; gap: 14px; color: var(--text-secondary); margin-bottom: 6px; flex-wrap: wrap; }
.tc-error {
  color: var(--color-danger); background: rgba(245, 74, 69, 0.06);
  padding: 6px 8px; border-radius: 6px; margin-bottom: 6px; word-break: break-all;
}
.tc-payload-block { border: 1px solid var(--border-color-lighter); border-radius: 6px; overflow: hidden; }
.tc-payload-head {
  display: flex; justify-content: space-between; padding: 4px 8px;
  background: rgba(0, 0, 0, 0.02); color: var(--text-secondary); font-size: 11px;
}
.tc-payload-hint { cursor: pointer; color: var(--color-primary); }
.tc-payload {
  margin: 0; padding: 8px; font-family: var(--font-mono); font-size: 11px;
  line-height: 1.55; color: var(--text-regular); white-space: pre-wrap;
  word-break: break-all; max-height: 260px; overflow: auto;
}
.tc-item.user .tc-dot { background: var(--color-warning); }
.tc-item.err .tc-dot { background: var(--color-danger); }
.tc-item.err .tc-name { color: var(--color-danger); }
.tc-item.done .tc-dot { background: var(--color-success); }
</style>
