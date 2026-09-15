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

      <!-- 时间线 -->
      <div class="tc-timeline">
        <div v-for="(item, i) in displayItems" :key="i"
             class="tc-item" :class="itemClass(item)">
          <div class="tc-item-head">
            <span class="tc-dot" />
            <span class="tc-name">{{ itemLabel(item) }}</span>
            <span v-if="item.durationMs != null" class="tc-dur">{{ fmtMs(item.durationMs) }}</span>
          </div>
          <div v-if="subLabel(item)" class="tc-sub">{{ subLabel(item) }}</div>
        </div>
      </div>
    </template>
  </a-modal>
</template>

<script setup lang="ts">
// 本轮链路弹窗(通用能力):查询完成后在对话内查看这一轮做了什么——
// 阶段时间线 + 每步耗时 + LLM 调用次数 + token 用量。
// 数据来自 GET /api/conversations/{id}/trace?turn=N(引擎级打点,任何 pack 可用)。
import { computed, ref, watch } from 'vue'
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

watch(visible, async (v) => {
  if (!v || !props.conversationId) return
  loading.value = true
  turn.value = null
  try {
    const data = await getConversationTrace(props.conversationId, props.turnIndex)
    turn.value = data.turns?.[0] || null
  } catch {
    turn.value = null
  } finally {
    loading.value = false
  }
})

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
.tc-item-head { display: flex; align-items: center; gap: 8px; }
.tc-name { font-size: 13px; color: var(--text-primary); font-weight: 500; }
.tc-dur { font-size: 11px; color: var(--text-secondary); margin-left: auto; font-family: var(--font-mono); }
.tc-sub { font-size: 11.5px; color: var(--text-secondary); margin-top: 2px; }
.tc-item.user .tc-dot { background: var(--color-warning); }
.tc-item.err .tc-dot { background: var(--color-danger); }
.tc-item.err .tc-name { color: var(--color-danger); }
.tc-item.done .tc-dot { background: var(--color-success); }
</style>
