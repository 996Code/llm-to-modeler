<template>
  <div class="ov">
    <div class="ov-head">
      <ApartmentOutlined class="ov-head-icon" />
      <span class="ov-head-text">链路总览</span>
      <span class="ov-hint">点击节点定位该轮明细</span>
    </div>
    <div class="ov-chain">
      <button v-for="turn in turns" :key="turn.index" type="button"
              class="ov-node" @click="$emit('locate', turn.index)">
        <span class="ov-rail" :class="'rs-' + (turn.status || 'ok')"></span>
        <div class="ov-body">
          <div class="ov-l1">
            <span class="ov-no">{{ turn.userContent === null ? '初始化' : `第 ${turn.index} 轮` }}</span>
            <span class="ov-q">{{ turn.userContent === null ? '会话初始化' : truncate(turn.userContent, 22) }}</span>
            <span class="ov-l1-right">
              <a-tooltip :title="STATUS_META[turn.status || 'ok'].tip">
                <component :is="STATUS_META[turn.status || 'ok'].icon"
                           :class="'ov-st-' + (turn.status || 'ok')" class="ov-st-icon" />
              </a-tooltip>
              <span class="ov-wall" :class="durClass(turn.wallMs)">{{ fmtMs(turn.wallMs) }}</span>
            </span>
          </div>
          <div class="ov-l2">
            <a-tag v-if="turn.pack" :color="packColor(turn.pack)" class="ov-pack" :title="turn.pack">
              {{ packTitle(turn.pack) || turn.pack }}
            </a-tag>
            <span v-else class="ov-nopack">未路由</span>
            <span v-if="turn.tool" class="ov-tool" :title="turn.tool">{{ turn.tool }}</span>
            <span v-if="turn.fallback" class="ov-fb">兜底</span>
            <span class="ov-dots">
              <span v-for="d in stageDots(turn)" :key="d.label" class="ov-dot"
                    :class="{ err: d.error }" :title="d.title">{{ d.label }}</span>
            </span>
            <span v-if="turn.llmCount" class="ov-llm">
              LLM {{ turn.llmCount }}<template v-if="turnTokens(turn)"> · {{ fmtTokens(turnTokens(turn)!) }} tok</template>
            </span>
          </div>
        </div>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
// 会话链路总览:每轮一个节点(插件/工具徽章 + 阶段序列 + 状态/耗时/token),
// 竖向连线成"链路图"。点击节点由父组件定位到下方该轮明细——扫一眼会话
// 形态、快速找坏轮,不用逐轮展开时间线。
import {
  ApartmentOutlined, CheckCircleFilled, ExclamationCircleFilled, CloseCircleFilled,
} from '@ant-design/icons-vue'
import type { Component } from 'vue'
import type { TraceItem, TraceTurn } from '../api'
import { packColor, packTitle } from '../packDisplay'

defineProps<{ turns: TraceTurn[] }>()
defineEmits<{ (e: 'locate', index: number): void }>()

const STATUS_META: Record<string, { icon: Component; tip: string }> = {
  ok: { icon: CheckCircleFilled, tip: '正常' },
  ask: { icon: ExclamationCircleFilled, tip: '追问挂起' },
  error: { icon: CloseCircleFilled, tip: '存在失败' },
}

function truncate(s: string | null, n: number): string {
  return s && s.length > n ? `${s.slice(0, n)}…` : (s || '')
}

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return '-'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function turnTokens(turn: TraceTurn): number | null {
  const total = (turn.promptTokens || 0) + (turn.completionTokens || 0)
  return total || null
}

function durClass(ms: number): string[] {
  if (ms >= 30000) return ['dur-red']
  if (ms >= 3000) return ['dur-orange']
  return []
}

function callFailed(it: TraceItem): boolean {
  return (it.statusCode != null && it.statusCode >= 400) || !!it.errorMessage
}

interface StageDot { label: string; error: boolean; title: string }

/** 轮内阶段序列(由 items 推导,只保留真实发生过的环节):路由→检索→工具→LLM→答 */
function stageDots(turn: TraceTurn): StageDot[] {
  const seen = { route: false, retrieve: false, tool: false, llm: false, answer: false }
  const err = { route: false, retrieve: false, tool: false }
  // 检索规模统计:轮数=向量查询次数(每轮混合检索以一次向量查询收口),
  // 召回=各次向量命中数合计——检索放大/零召回在总览层即可报警
  let graphCalls = 0
  let fulltextCalls = 0
  let vectorCalls = 0
  let recallTotal = 0
  for (const it of turn.items) {
    if (it.type === 'event') {
      if (it.kind === 'trace') {
        const p = it.payload || {}
        if (p.stage === 'intent_route') {
          seen.route = true
          // 兜底是设计内降级(未命中→KB 链路),路由点不标红
          if (p.status === 'error' && !turn.fallback) err.route = true
        } else if (p.stage === 'tool_execute' || p.stage === 'preflight') {
          seen.tool = true
          if (p.status === 'error') err.tool = true
        }
      } else if (it.kind === 'assistant') {
        seen.answer = true
      }
    } else if (it.callType === 'vector') {
      seen.retrieve = true
      vectorCalls++
      const r = it.responseData as { hits?: number | null } | null | undefined
      if (r && typeof r.hits === 'number') recallTotal += r.hits
      if (callFailed(it)) err.retrieve = true
    } else if (it.callType === 'graph') {
      seen.retrieve = true
      graphCalls++
      if (callFailed(it)) err.retrieve = true
    } else if (it.callType === 'fulltext') {
      seen.retrieve = true
      fulltextCalls++
      if (callFailed(it)) err.retrieve = true
    } else if (it.callType === 'llm') {
      seen.llm = true
    } else if (it.callType === 'upstream') {
      seen.tool = true
      if (callFailed(it)) err.tool = true
    }
  }
  const dots: StageDot[] = []
  if (seen.route) {
    dots.push(turn.fallback
      ? { label: '路由', error: false, title: '路由未命中，走知识库兜底' }
      : { label: '路由', error: err.route, title: err.route ? '路由失败' : '意图路由' })
  }
  if (seen.retrieve) {
    const rounds = vectorCalls || graphCalls || fulltextCalls
    const label = rounds > 1
      ? `检索×${rounds}轮${recallTotal ? `/召回${recallTotal}` : ''}`
      : '检索'
    const breakdown = [
      `向量 ${vectorCalls} 次${recallTotal ? ` · 召回 ${recallTotal} 条` : ''}`,
      `图谱 ${graphCalls} 次`,
      `全文 ${fulltextCalls} 次`,
    ].join(' / ')
    dots.push({
      label,
      error: err.retrieve,
      title: (err.retrieve ? '检索调用失败：' : '') + breakdown,
    })
  }
  if (seen.tool) dots.push({ label: '工具', error: err.tool, title: err.tool ? '工具执行失败' : '工具执行' })
  if (seen.llm) dots.push({ label: 'LLM', error: false, title: 'LLM 调用' })
  if (seen.answer) dots.push({ label: '答', error: false, title: '助手回复' })
  return dots
}
</script>

<style scoped>
.ov { border: 1px solid #eef0f4; border-radius: 10px; background: #fff; margin: 14px 0 16px; }
.ov-head {
  display: flex; align-items: center; gap: 6px;
  padding: 9px 14px; border-bottom: 1px solid #f1f3f7;
  font-weight: 600; color: #1f2937; font-size: 13px;
}
.ov-head-icon { color: var(--brand); }
.ov-hint { margin-left: auto; font-weight: 400; font-size: 11px; color: #9ca3af; }

/* 链路容器限高滚动(管理端抽屉限高约定);轮多时不出横向滚动 */
.ov-chain { max-height: 260px; overflow-y: auto; padding: 6px 6px 8px; }

.ov-node {
  position: relative; display: flex; width: 100%;
  padding: 7px 10px 7px 20px; border: none; background: transparent;
  border-radius: 8px; cursor: pointer; text-align: left;
}
.ov-node:hover { background: #f3f6ff; }
/* 左侧连线圆点(同明细时间线 .tr-rail 视觉),颜色随轮状态 */
.ov-rail { position: absolute; left: 8px; top: 14px; width: 8px; height: 8px; border-radius: 50%; }
.ov-rail::after {
  content: ''; position: absolute; left: 3px; top: 9px; bottom: -16px; width: 2px; background: #eef0f4;
}
.ov-node:last-child .ov-rail::after { display: none; }
.rs-ok { background: #22c55e; }
.rs-ask { background: #f59e0b; }
.rs-error { background: #dc2626; }

.ov-body { flex: 1; min-width: 0; }
.ov-l1 { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ov-no { font-weight: 600; font-size: 12px; color: #1f2937; flex-shrink: 0; }
.ov-q {
  flex: 1; min-width: 0; font-size: 12px; color: #6b7280;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ov-l1-right { display: inline-flex; align-items: center; gap: 7px; flex-shrink: 0; }
.ov-st-icon { font-size: 13px; }
.ov-st-ok { color: #22c55e; }
.ov-st-ask { color: #f59e0b; }
.ov-st-error { color: #dc2626; }
.ov-wall { font-size: 12px; font-weight: 600; color: #374151; font-variant-numeric: tabular-nums; }
.dur-orange { color: #ea580c; }
.dur-red { color: #dc2626; }

.ov-l2 { display: flex; align-items: center; gap: 6px; margin-top: 3px; min-width: 0; flex-wrap: wrap; }
.ov-pack { font-size: 11px; line-height: 18px; margin-inline-end: 0; flex-shrink: 0; }
.ov-nopack { font-size: 11px; color: #9ca3af; }
.ov-tool {
  font-size: 11px; color: var(--brand-ink); background: var(--brand-tint);
  border-radius: 6px; padding: 0 7px; line-height: 18px;
  max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ov-fb { font-size: 11px; color: #b45309; background: #fffbeb; border-radius: 6px; padding: 0 7px; line-height: 18px; }
.ov-dots { display: inline-flex; align-items: center; font-size: 11px; color: #6b7280; }
.ov-dot { white-space: nowrap; }
.ov-dot + .ov-dot::before { content: '›'; margin: 0 4px; color: #c3c8d4; }
.ov-dot.err { color: #dc2626; font-weight: 600; }
.ov-llm { margin-left: auto; font-size: 11px; color: #6d28d9; font-variant-numeric: tabular-nums; flex-shrink: 0; }
</style>
