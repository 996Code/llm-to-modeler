<template>
  <div v-if="trace">
    <!-- 汇总条 -->
    <div class="tr-summary">
      <div class="tr-chip"><MessageOutlined /> {{ trace.summary.turns }} 轮</div>
      <div class="tr-chip llm"><RobotOutlined /> LLM {{ trace.summary.llmCalls }} 次 / {{ fmtMs(trace.summary.llmMs) }}</div>
      <div class="tr-chip up"><CloudServerOutlined /> 上游 {{ trace.summary.upstreamCalls }} 次 / {{ fmtMs(trace.summary.upstreamMs) }}</div>
      <div class="tr-chip dot"><NodeIndexOutlined /> 打点 {{ trace.summary.traceEvents }}</div>
      <div class="tr-chip time" v-if="trace.summary.firstAt">{{ fmtTime(trace.summary.firstAt) }} ~ {{ fmtTime(trace.summary.lastAt) }}</div>
    </div>

    <!-- 轮次卡片 -->
    <div class="tr-turns">
      <div v-for="turn in trace.turns" :key="turn.index" class="tr-turn">
        <button class="tr-turn-head" @click="toggleTurn(turn.index)">
          <span class="tr-caret" :class="{ open: openTurns.includes(turn.index) }">▸</span>
          <span v-if="turn.userContent === null" class="tr-turn-name">会话初始化</span>
          <span v-else class="tr-turn-name">第 {{ turn.index }} 轮<span class="tr-turn-msg">「{{ truncate(turn.userContent, 26) }}」</span></span>
          <span class="tr-turn-badges">
            <span v-if="turn.userContent !== null" class="tb">墙钟 <b>{{ fmtMs(turn.wallMs) }}</b></span>
            <span v-if="turn.llmCount" class="tb llm">LLM {{ turn.llmCount }}/{{ fmtMs(turn.llmMs) }}</span>
            <span v-if="turn.upstreamCount" class="tb up">上游 {{ turn.upstreamCount }}/{{ fmtMs(turn.upstreamMs) }}</span>
          </span>
        </button>

        <div v-if="openTurns.includes(turn.index)" class="tr-timeline">
          <div v-for="(item, idx) in turn.items" :key="idx"
               class="tr-item" :class="['tr-' + itemLineClass(item), { open: expandedRows.has(rowKey(turn.index, idx)) }]">
            <span class="tr-rail"></span>
            <div class="tr-item-body">
              <!-- 用户 / 助手:内容锚点,气泡直接展示 -->
              <template v-if="item.type === 'event' && (item.kind === 'user' || item.kind === 'assistant')">
                <div class="tr-item-head flat">
                  <span class="tr-tag" :class="item.kind === 'user' ? 'tg-user' : 'tg-assistant'">
                    {{ item.kind === 'user' ? '用户' : '助手' }}
                  </span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                </div>
                <div class="tr-bubble" :class="item.kind === 'user' ? 'bb-user' : 'bb-assistant'">
                  {{ item.payload?.content }}
                </div>
              </template>

              <!-- 打点(trace) / 系统事件:点行头展开 -->
              <template v-else-if="item.type === 'event'">
                <div class="tr-item-head" @click="toggleItem(turn.index, idx)">
                  <span class="tr-tag" :class="item.kind === 'trace' ? ('tg-' + (item.payload?.status || 'info')) : 'tg-sys'">
                    {{ item.kind === 'trace' ? (item.payload?.title || item.payload?.stage) : eventLabel(item.kind) }}
                  </span>
                  <span v-if="item.kind === 'trace' && item.payload?.duration_ms != null" class="tr-dur">{{ fmtMs(item.payload.duration_ms) }}</span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                  <DownOutlined class="tr-arrow" :class="{ open: expandedRows.has(rowKey(turn.index, idx)) }" />
                </div>
                <div v-if="expandedRows.has(rowKey(turn.index, idx))" class="tr-detail">
                  <JsonViewer v-if="hasKeys(item.payload?.detail ?? item.payload)" label="明细"
                    :data="item.payload?.detail ?? item.payload" :height="200" :default-collapsed="false" />
                </div>
              </template>

              <!-- LLM / 上游调用:点行头展开格式化明细(一键切原始) -->
              <template v-else-if="item.type === 'call'">
                <div class="tr-item-head" @click="toggleItem(turn.index, idx)">
                  <span class="tr-tag" :class="CALL_TYPE_CLS[item.callType || ''] || 'tg-sys'">
                    {{ CALL_TYPE_META[item.callType || '']?.label || item.callType }} · {{ stageLabel(item.stage, item.endpoint) }}
                  </span>
                  <span v-if="recallSummary(item)" class="tr-recall">{{ recallSummary(item) }}</span>
                  <span v-if="item.errorMessage" class="tr-err">{{ truncate(item.errorMessage, 60) }}</span>
                  <span class="tr-dur" :class="durClass(item.durationMs)">{{ fmtMs(item.durationMs ?? null) }}</span>
                  <a-tag v-if="item.statusCode != null && item.statusCode >= 400" color="red" class="tr-code">{{ item.statusCode }}</a-tag>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                  <DownOutlined class="tr-arrow" :class="{ open: expandedRows.has(rowKey(turn.index, idx)) }" />
                </div>
                <div v-if="expandedRows.has(rowKey(turn.index, idx))" class="tr-detail">
                  <div class="tr-detail-bar">
                    <a class="tr-raw-toggle" @click.stop="toggleRaw(turn.index, idx)">
                      {{ rawRows.has(rowKey(turn.index, idx)) ? '⇤ 格式化明细' : '原始数据 →' }}
                    </a>
                  </div>
                  <TraceDetailSections v-if="!rawRows.has(rowKey(turn.index, idx))"
                    :sections="formatDetail(item as any)" />
                  <template v-else>
                    <JsonViewer label="请求" :data="item.requestData" :height="220" :default-collapsed="false" />
                    <JsonViewer label="响应" :data="item.responseData" :height="260" :default-collapsed="false" />
                  </template>
                </div>
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 当前制品 -->
    <template v-if="trace.conversation.currentConfig">
      <div class="tr-config-head"><FileTextOutlined /> 当前制品配置</div>
      <JsonViewer label="currentConfig" :data="trace.conversation.currentConfig" :height="360" />
    </template>
  </div>

  <a-spin v-else-if="loading" style="display: block; padding: 60px; text-align: center" />
  <a-empty v-else description="无链路数据" style="padding: 60px 0" />
</template>

<script setup lang="ts">
// 会话链路追踪视图:轮次卡片 + 时间线。行头点击展开该条明细(call 默认
// 格式化展示,一键切原始 JSON),收起时一行一标签,时间线一眼可读。
import { inject, onMounted, ref, watch } from 'vue'
import {
  MessageOutlined, RobotOutlined, CloudServerOutlined, NodeIndexOutlined, FileTextOutlined,
  DownOutlined,
} from '@ant-design/icons-vue'
import { ConversationTrace as TraceType, fetchConversationTrace, fmtTime } from '../api'
import type { LoadSafely } from './loadSafely'
import JsonViewer from './JsonViewer.vue'
import { formatDetail } from '../../shared/traceDetail'
import TraceDetailSections from '../../shared/TraceDetailSections.vue'
import { CALL_TYPE_META, CALL_TYPE_CLS, stageLabel } from '../labels'

/** 行展开态与原始数据视图:key = 轮次:行号(跨轮不撞) */
const expandedRows = ref(new Set<string>())
const rawRows = ref(new Set<string>())
const rowKey = (turn: number, idx: number) => `${turn}:${idx}`
function toggleItem(turn: number, idx: number) {
  const key = rowKey(turn, idx)
  const s = new Set(expandedRows.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  expandedRows.value = s
}
function toggleRaw(turn: number, idx: number) {
  const key = rowKey(turn, idx)
  const s = new Set(rawRows.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  rawRows.value = s
}

/** 图/向量检索的召回摘要(图谱=种子/节点/边,向量=命中数+top 分数) */
function recallSummary(item: { callType?: string; responseData?: unknown }): string {
  if (item.callType !== 'graph' && item.callType !== 'vector') return ''
  const r = item.responseData as
    | { hits?: number; nodes?: number; edges?: number; topScore?: number | null; termDetail?: unknown[] } | null
  if (!r) return ''
  if (item.callType === 'graph') {
    if (r.hits != null) return `种子命中 ${r.hits}/${(r.termDetail || []).length} 词`
    if (r.nodes != null) return `召回 ${r.nodes} 节点 / ${r.edges ?? 0} 边`
  }
  if (item.callType === 'vector' && r.hits != null) {
    return `召回 ${r.hits} 条${r.topScore != null ? ` · top ${r.topScore}` : ''}`
  }
  return ''
}

const props = defineProps<{ convId: string }>()

const loadSafely = inject<LoadSafely>('loadSafely')!
const trace = ref<TraceType | null>(null)
const loading = ref(false)
const openTurns = ref<number[]>([])

function eventLabel(kind: string | undefined): string {
  const map: Record<string, string> = {
    checkpoint: '快照', compacted: '历史压缩点', compact_trace: '压缩轨迹', ask: '追问现场',
  }
  return map[kind || ''] || kind || '事件'
}

function itemLineClass(item: { type: string; kind?: string; callType?: string }): string {
  if (item.type === 'call') {
    if (item.callType === 'upstream') return 'upcall'
    if (item.callType === 'graph') return 'graphcall'
    if (item.callType === 'vector') return 'vectorcall'
    if (item.callType === 'fulltext') return 'fulltextcall'
    return 'call'
  }
  switch (item.kind) {
    case 'user': return 'user'
    case 'assistant': return 'assistant'
    case 'trace': return 'trace'
    default: return 'sys'
  }
}

function durClass(ms: number | null | undefined): string[] {
  if (ms == null) return []
  if (ms >= 30000) return ['dur-red']
  if (ms >= 3000) return ['dur-orange']
  return []
}

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return '-'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function truncate(s: string, n: number): string {
  return s && s.length > n ? `${s.slice(0, n)}…` : (s || '')
}

function hasKeys(d: unknown): boolean {
  return !!d && typeof d === 'object' && Object.keys(d as object).length > 0
}

function toggleTurn(index: number) {
  const i = openTurns.value.indexOf(index)
  if (i >= 0) openTurns.value.splice(i, 1)
  else openTurns.value.push(index)
}

async function load() {
  loading.value = true
  await loadSafely(async () => {
    trace.value = await fetchConversationTrace(props.convId)
    const turns = trace.value?.turns ?? []
    if (turns.length) openTurns.value = [turns[turns.length - 1].index]
  })
  loading.value = false
}

onMounted(load)
watch(() => props.convId, load)
</script>

<style scoped>
.tr-summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 16px; }
.tr-chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; color: #4b5563; background: #f3f4f6;
  border-radius: 999px; padding: 3px 12px;
}
.tr-chip.llm { background: #f5f3ff; color: #6d28d9; }
.tr-chip.up { background: #ecfeff; color: #0e7490; }
.tr-chip.dot { background: #fff7ed; color: #c2410c; }
.tr-chip.time { background: transparent; color: #9ca3af; padding-left: 4px; }

.tr-turns { display: flex; flex-direction: column; gap: 10px; }
.tr-turn { border: 1px solid #eef0f4; border-radius: 10px; overflow: hidden; background: #fff; }
.tr-turn-head {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 10px 14px; background: #fafbfd; border: none; cursor: pointer; text-align: left;
}
.tr-turn-head:hover { background: #f3f6ff; }
.tr-caret { color: #9ca3af; transition: transform 0.15s; display: inline-block; }
.tr-caret.open { transform: rotate(90deg); }
.tr-turn-name { font-weight: 600; font-size: 13px; color: #1f2937; }
.tr-turn-msg { font-weight: 400; color: #6b7280; }
.tr-turn-badges { margin-left: auto; display: flex; gap: 6px; }
.tb {
  font-size: 11px; color: #6b7280; background: #eef0f4; border-radius: 6px; padding: 1px 8px;
}
.tb b { color: #374151; }
.tb.llm { background: #f5f3ff; color: #6d28d9; }
.tb.up { background: #ecfeff; color: #0e7490; }

.tr-timeline { padding: 6px 14px 14px 18px; }
.tr-item { position: relative; padding: 4px 0 4px 18px; border-radius: 8px; }
.tr-rail {
  position: absolute; left: 0; top: 13px; width: 8px; height: 8px; border-radius: 50%;
}
.tr-rail::after {
  content: ''; position: absolute; left: 3px; top: 12px; bottom: -12px; width: 2px;
  background: #eef0f4;
}
.tr-item:last-child .tr-rail::after { display: none; }
.tr-user .tr-rail { background: #22c55e; }
.tr-assistant .tr-rail { background: #3b82f6; }
.tr-trace .tr-rail { background: #f59e0b; }
.tr-call .tr-rail { background: #8b5cf6; }
.tr-upcall .tr-rail { background: #06b6d4; }
.tr-graphcall .tr-rail { background: #3b82f6; }
.tr-vectorcall .tr-rail { background: #22c55e; }
.tr-fulltextcall .tr-rail { background: #f97316; }
.tr-sys .tr-rail { background: #cbd5e1; }

/* 行头:可点击展开的行 hover 有反馈,展开行左侧高亮 */
.tr-item-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 3px 6px; margin: 0 -6px; border-radius: 6px; cursor: pointer; user-select: none;
}
.tr-item-head:hover { background: #f5f7fb; }
.tr-item-head.flat { cursor: default; padding: 3px 6px; margin-top: 4px; }
.tr-item-head.flat:hover { background: transparent; }
.tr-item.open .tr-item-head { background: #f5f7fb; }
.tr-tag {
  font-size: 12px; border-radius: 6px; padding: 1px 8px; font-weight: 500; line-height: 18px;
}
.tg-user { background: #f0fdf4; color: #15803d; }
.tg-assistant { background: #eff6ff; color: #1d4ed8; }
.tg-llm { background: #f5f3ff; color: #6d28d9; }
.tg-up { background: #ecfeff; color: #0e7490; }
.tg-ok { background: #f0fdf4; color: #15803d; }
.tg-error { background: #fef2f2; color: #b91c1c; }
.tg-ask { background: #fffbeb; color: #b45309; }
.tg-info { background: #eff6ff; color: #1d4ed8; }
.tg-sys { background: #f1f5f9; color: #64748b; }
.tg-graph { background: #eff6ff; color: #1d4ed8; }
.tg-vector { background: #f0fdf4; color: #15803d; }
.tg-fulltext { background: #fff7ed; color: #c2410c; }
.tr-recall {
  font-size: 11px; color: #2563eb; background: #f0f5ff;
  border-radius: 6px; padding: 1px 8px; font-variant-numeric: tabular-nums;
}
.tr-ts { font-size: 11px; color: #c3c8d4; }
.tr-dur { font-size: 12px; font-weight: 600; color: #374151; margin-left: auto; }
.dur-orange { color: #ea580c; }
.dur-red { color: #dc2626; }
.tr-code { font-size: 11px; line-height: 16px; }
.tr-err { font-size: 12px; color: #ef4444; }
.tr-arrow { font-size: 10px; color: #b6bdc9; flex-shrink: 0; transition: transform 0.15s; }
.tr-arrow.open { transform: rotate(180deg); }
.tr-bubble {
  margin-top: 5px; padding: 7px 10px; border-radius: 8px; font-size: 13px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.bb-user { background: #f0fdf4; color: #14532d; }
.bb-assistant { background: #eff6ff; color: #1e3a8a; }

/* 展开明细:浅卡片承载,切换链接置右上 */
.tr-detail {
  margin: 6px 0 4px; padding: 10px 12px; background: #f8fafc;
  border: 1px solid #eef2f7; border-radius: 8px;
}
.tr-detail-bar { display: flex; justify-content: flex-end; margin-bottom: 2px; }
.tr-raw-toggle { font-size: 11px; color: #3370ff; cursor: pointer; }
.tr-config-head {
  display: flex; align-items: center; gap: 6px; margin: 18px 0 4px;
  font-weight: 600; color: #1f2937; font-size: 14px;
}
</style>
