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
          <div v-for="(item, idx) in turn.items" :key="idx" class="tr-item" :class="'tr-' + itemLineClass(item)">
            <span class="tr-rail"></span>
            <div class="tr-item-body">
              <!-- 用户 / 助手 -->
              <template v-if="item.type === 'event' && (item.kind === 'user' || item.kind === 'assistant')">
                <div class="tr-item-head">
                  <span class="tr-tag" :class="item.kind === 'user' ? 'tg-user' : 'tg-assistant'">
                    {{ item.kind === 'user' ? '用户' : '助手' }}
                  </span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                </div>
                <div class="tr-bubble" :class="item.kind === 'user' ? 'bb-user' : 'bb-assistant'">
                  {{ item.payload?.content }}
                </div>
              </template>

              <!-- 打点(trace) -->
              <template v-else-if="item.type === 'event' && item.kind === 'trace'">
                <div class="tr-item-head">
                  <span class="tr-tag" :class="'tg-' + (item.payload?.status || 'info')">
                    {{ item.payload?.title || item.payload?.stage }}
                  </span>
                  <span v-if="item.payload?.duration_ms != null" class="tr-dur">{{ item.payload.duration_ms }}ms</span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                </div>
                <JsonViewer v-if="hasKeys(item.payload?.detail)" label="明细" :data="item.payload?.detail"
                  :height="200" :default-collapsed="true" />
              </template>

              <!-- LLM / 上游调用 -->
              <template v-else-if="item.type === 'call'">
                <div class="tr-item-head">
                  <span class="tr-tag" :class="item.callType === 'llm' ? 'tg-llm' : 'tg-up'">
                    {{ item.callType === 'llm' ? 'LLM' : '上游' }} · {{ stageLabel(item.stage, item.endpoint) }}
                  </span>
                  <span class="tr-dur" :class="durClass(item.durationMs)">{{ item.durationMs ?? '-' }}ms</span>
                  <a-tag v-if="item.statusCode != null" :color="item.statusCode < 400 ? 'green' : 'red'" class="tr-code">
                    {{ item.statusCode }}
                  </a-tag>
                  <span v-if="item.errorMessage" class="tr-err">{{ truncate(item.errorMessage, 60) }}</span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                </div>
                <JsonViewer label="请求" :data="item.requestData" :height="220" :default-collapsed="true" />
                <JsonViewer label="响应" :data="item.responseData" :height="260" :default-collapsed="true" />
              </template>

              <!-- 系统事件:checkpoint / compacted / compact_trace / ask -->
              <template v-else>
                <div class="tr-item-head">
                  <span class="tr-tag tg-sys">{{ eventLabel(item.kind) }}</span>
                  <span class="tr-ts">{{ fmtTime(item.at) }}</span>
                </div>
                <JsonViewer label="载荷" :data="item.payload" :height="180" :default-collapsed="true" />
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
// 会话链路追踪视图(视觉升级版):轮次卡片 + 时间线 + JsonViewer。
import { inject, onMounted, ref, watch } from 'vue'
import {
  MessageOutlined, RobotOutlined, CloudServerOutlined, NodeIndexOutlined, FileTextOutlined,
} from '@ant-design/icons-vue'
import { ConversationTrace as TraceType, fetchConversationTrace, fmtTime } from '../api'
import type { LoadSafely } from './loadSafely'
import JsonViewer from './JsonViewer.vue'

const props = defineProps<{ convId: string }>()

const loadSafely = inject<LoadSafely>('loadSafely')!
const trace = ref<TraceType | null>(null)
const loading = ref(false)
const openTurns = ref<number[]>([])

const STAGE_LABELS: Record<string, string> = {
  route_pack: '意图路由·选领域',
  route_tool: '意图路由·选工具',
  compress_history: '历史压缩',
  'create_form.parse': '表单解析',
  'create_form.generate': '表单生成',
  'get_form.parse': '表单码解析',
  'image_form.analyze': '图片识别',
  'image_form.generate': '图片转配置',
  'clone_form.parse': '克隆解析',
  'chat.reply': '闲聊回复',
  'submit_leave.parse': '请假信息提取',
}

function stageLabel(stage: string | null | undefined, fallback: string | undefined): string {
  if (stage && STAGE_LABELS[stage]) return STAGE_LABELS[stage]
  return stage || fallback || '调用'
}

function eventLabel(kind: string | undefined): string {
  const map: Record<string, string> = {
    checkpoint: '快照', compacted: '历史压缩点', compact_trace: '压缩轨迹', ask: '追问现场',
  }
  return map[kind || ''] || kind || '事件'
}

function itemLineClass(item: { type: string; kind?: string; callType?: string }): string {
  if (item.type === 'call') return item.callType === 'upstream' ? 'upcall' : 'call'
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

function fmtMs(ms: number): string {
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

.tr-timeline { padding: 4px 14px 12px 18px; }
.tr-item { position: relative; padding: 7px 0 7px 18px; }
.tr-rail {
  position: absolute; left: 0; top: 14px; width: 8px; height: 8px; border-radius: 50%;
}
.tr-rail::after {
  content: ''; position: absolute; left: 3px; top: 12px; bottom: -14px; width: 2px;
  background: #eef0f4;
}
.tr-item:last-child .tr-rail::after { display: none; }
.tr-user .tr-rail { background: #22c55e; }
.tr-assistant .tr-rail { background: #3b82f6; }
.tr-trace .tr-rail { background: #f59e0b; }
.tr-call .tr-rail { background: #8b5cf6; }
.tr-upcall .tr-rail { background: #06b6d4; }
.tr-sys .tr-rail { background: #cbd5e1; }

.tr-item-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tr-tag {
  font-size: 12px; border-radius: 6px; padding: 1px 8px; font-weight: 500;
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
.tr-ts { font-size: 11px; color: #c3c8d4; }
.tr-dur { font-size: 12px; font-weight: 600; color: #374151; }
.dur-orange { color: #ea580c; }
.dur-red { color: #dc2626; }
.tr-code { font-size: 11px; line-height: 16px; }
.tr-err { font-size: 12px; color: #ef4444; }
.tr-bubble {
  margin-top: 5px; padding: 7px 10px; border-radius: 8px; font-size: 13px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.bb-user { background: #f0fdf4; color: #14532d; }
.bb-assistant { background: #eff6ff; color: #1e3a8a; }
.tr-config-head {
  display: flex; align-items: center; gap: 6px; margin: 18px 0 4px;
  font-weight: 600; color: #1f2937; font-size: 14px;
}
</style>
