<template>
  <div>
    <div class="tk-toolbar">
      <a-select v-model:value="filterStatus" style="width: 130px" @change="search">
        <a-select-option value="">全部状态</a-select-option>
        <a-select-option value="running">运行中</a-select-option>
        <a-select-option value="pending">等待中</a-select-option>
        <a-select-option value="succeeded">已成功</a-select-option>
        <a-select-option value="failed">已失败</a-select-option>
        <a-select-option value="cancelled">已取消</a-select-option>
        <a-select-option value="interrupted">已中断</a-select-option>
      </a-select>
      <a-select v-if="types.length" v-model:value="filterType" style="width: 220px" @change="search">
        <a-select-option value="">全部类型</a-select-option>
        <a-select-option v-for="t in types" :key="t.type" :value="t.type">{{ typeLabel(t) }}</a-select-option>
      </a-select>
      <a-button type="primary" @click="search">查询</a-button>
      <a-button @click="load">刷新</a-button>
      <a-switch v-model:checked="autoRefresh" checked-children="自动刷新" un-checked-children="暂停" size="small" />
      <span class="tk-count">共 {{ total }} 条</span>
    </div>

    <a-table
      :columns="columns"
      :data-source="rows"
      :pagination="pagination"
      :loading="loading"
      row-key="id"
      size="middle"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'taskType'">
          <a-tag color="geekblue">{{ record.taskType }}</a-tag>
        </template>
        <template v-else-if="column.key === 'title'">
          <div class="tk-title" :title="record.title">{{ record.title || record.taskType }}</div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'progress'">
          <a-progress
            v-if="record.status === 'running' || record.status === 'pending'"
            :percent="record.progress" size="small" status="active"
            :format="(p: number) => `${p}%`"
          />
          <span v-else-if="record.status === 'succeeded'" class="tk-done">100%</span>
          <span v-else>-</span>
        </template>
        <template v-else-if="column.key === 'createdAt'">
          <span class="tk-time">{{ fmtTime(record.createdAt) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a @click="openLogs(record)">日志</a>
          <a-divider type="vertical" />
          <a-popconfirm
            v-if="record.status === 'running' || record.status === 'pending'"
            title="确认取消该任务?" @confirm="doCancel(record)"
          >
            <a style="color: #dc2626">取消</a>
          </a-popconfirm>
          <span v-else style="color: #c7cdd8">取消</span>
        </template>
      </template>
    </a-table>

    <!-- 任务日志抽屉:SSE 实时滚动 + 历史回放 -->
    <a-drawer
      v-model:open="logsOpen"
      width="720"
      :title="activeTask ? `${activeTask.title || activeTask.taskType} · 任务日志` : '任务日志'"
    >
      <template v-if="activeTask">
        <div class="tk-drawer-head">
          <a-tag :color="statusColor(activeTask.status)">{{ statusLabel(activeTask.status) }}</a-tag>
          <a-progress
            :percent="activeTask.progress" size="small" style="flex: 1"
            :status="activeTask.status === 'failed' ? 'exception' : undefined"
          />
          <span v-if="activeTask.progressMessage" class="tk-msg">{{ activeTask.progressMessage }}</span>
        </div>
        <a-alert
          v-if="activeTask.error" type="error" show-icon style="margin-bottom: 10px"
          :message="activeTask.error"
        />
        <div class="tk-logs-toolbar">
          <a-radio-group v-model:value="levelFilter" size="small" button-style="solid">
            <a-radio-button value="">全部</a-radio-button>
            <a-radio-button value="info">info</a-radio-button>
            <a-radio-button value="warn">warn</a-radio-button>
            <a-radio-button value="error">error</a-radio-button>
          </a-radio-group>
          <a-radio-group v-model:value="viewMode" size="small">
            <a-radio-button value="structured"><AppstoreOutlined /> 结构化</a-radio-button>
            <a-radio-button value="raw"><BarsOutlined /> 原始</a-radio-button>
          </a-radio-group>
          <span class="tk-logs-count">{{ filteredLogs.length }} 条</span>
        </div>

        <!-- 结构化视图:按事件阶段分组,逐块进度条 + 关键指标徽章 -->
        <div v-if="viewMode === 'structured'" class="tk-logs" ref="logsBox">
          <template v-for="g in structuredGroups" :key="g.key">
            <div class="tk-group-head">
              <span class="tk-group-title">{{ g.title }}</span>
              <span v-if="g.badge" class="tk-group-badge">{{ g.badge }}</span>
              <span class="tk-group-time">{{ fmtTime(g.logs[0].createdAt).slice(11) }}</span>
            </div>
            <!-- 逐块抽取组:进度条式行 -->
            <div v-for="lg in g.logs" :key="lg.id" class="tk-row" :class="`tk-row-${lg.level}`">
              <template v-if="g.key === 'chunks'">
                <div class="tk-chunk-row">
                  <span class="tk-chunk-id">块{{ chunkData(lg).chunk ?? '?' }}</span>
                  <a-progress
                    :percent="chunkPct(lg)" size="small" style="flex: 1; min-width: 60px"
                    :status="lg.level === 'warn' ? 'exception' : undefined"
                  />
                  <span class="tk-chunk-metric">{{ chunkData(lg).entities ?? 0 }}e / {{ chunkData(lg).relations ?? 0 }}r</span>
                  <span class="tk-chunk-dur">{{ fmtMs(chunkData(lg).duration_ms) }}</span>
                </div>
              </template>
              <!-- 其余组:标题行 + 指标徽章 -->
              <template v-else>
                <div class="tk-line">
                  <a-tag v-if="lg.level !== 'info'" class="tk-log-level" :color="lg.level === 'error' ? 'red' : 'orange'">{{ lg.level }}</a-tag>
                  <span class="tk-line-msg">{{ lg.message }}</span>
                </div>
                <div v-if="metricsOf(lg).length" class="tk-metrics">
                  <span v-for="m in metricsOf(lg)" :key="m.k" class="tk-metric">
                    <span class="tk-metric-k">{{ m.k }}</span><b class="tk-metric-v">{{ m.v }}</b>
                  </span>
                </div>
              </template>
            </div>
          </template>
          <a-empty v-if="!structuredGroups.length" description="暂无日志" style="padding: 40px 0" />
        </div>

        <!-- 原始视图:纯文本流(时间/级别/消息/data) -->
        <div v-else class="tk-logs" ref="logsBox">
          <div v-for="lg in filteredLogs" :key="lg.id" class="tk-log-line">
            <span class="tk-log-time">{{ fmtTime(lg.createdAt).slice(11) }}</span>
            <a-tag
              class="tk-log-level" :color="lg.level === 'error' ? 'red' : lg.level === 'warn' ? 'orange' : 'blue'"
              style="margin-inline-end: 6px"
            >{{ lg.level }}</a-tag>
            <span class="tk-log-msg">
              {{ lg.message }}
              <span v-if="fmtData(lg)" class="tk-log-data">{{ fmtData(lg) }}</span>
            </span>
          </div>
          <a-empty v-if="!filteredLogs.length" description="暂无日志" style="padding: 40px 0" />
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 任务中心:任务表格(进度条/状态) + 日志抽屉(SSE 实时 + 断流降级轮询)。
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { AppstoreOutlined, BarsOutlined } from '@ant-design/icons-vue'
import {
  TaskItem, TaskLogItem, TaskStatus, TaskTypeItem,
  cancelTask, fetchTask, fetchTaskLogs, fetchTasks, fetchTaskTypes, fmtTime, streamTaskEvents,
} from '../api'
import type { LoadSafely } from './loadSafely'

const loadSafely = inject<LoadSafely>('loadSafely')!

const rows = ref<TaskItem[]>([])
const total = ref(0)
const loading = ref(false)
const filterStatus = ref('')
const filterType = ref('')
const types = ref<TaskTypeItem[]>([])
const autoRefresh = ref(true)

const page = reactive({ current: 1, pageSize: 20 })
const pagination = computed(() => ({
  total: total.value,
  current: page.current,
  pageSize: page.pageSize,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50', '100'],
  showTotal: (t: number) => `共 ${t} 条`,
}))

const columns = [
  { title: '时间', key: 'createdAt', width: 165 },
  { title: '类型', key: 'taskType', width: 190 },
  { title: '标题', key: 'title', ellipsis: true },
  { title: '插件', key: 'packName', width: 140 },
  { title: '状态', key: 'status', width: 90 },
  { title: '进度', key: 'progress', width: 170 },
  { title: '操作', key: 'actions', width: 120 },
]

const _statusColor: Record<TaskStatus, string> = {
  pending: 'default', running: 'processing', succeeded: 'success',
  failed: 'error', cancelled: 'warning', interrupted: 'warning',
}
const _statusLabel: Record<TaskStatus, string> = {
  pending: '等待中', running: '运行中', succeeded: '已成功',
  failed: '已失败', cancelled: '已取消', interrupted: '已中断',
}
function statusColor(s: TaskStatus): string {
  return _statusColor[s] ?? 'default'
}
function statusLabel(s: TaskStatus): string {
  return _statusLabel[s] ?? String(s)
}
function typeLabel(t: TaskTypeItem): string {
  return t.packName ? `${t.type} (${t.packName})` : t.type
}

// ── 列表 ──────────────────────────────────────────────

let listSeq = 0   // 请求序号守卫:5s 自动刷新与手动翻页并发时,晚到的旧页响应不得覆盖新页

async function load() {
  const seq = ++listSeq
  loading.value = true
  await loadSafely(async () => {
    const data = await fetchTasks({
      limit: page.pageSize,
      offset: (page.current - 1) * page.pageSize,
      status: filterStatus.value || undefined,
      type: filterType.value || undefined,
    })
    if (seq !== listSeq) return   // 已有更新请求在途/完成,丢弃本次过期结果
    rows.value = data.items
    total.value = data.total
    // 抽屉打开且当前任务在列表里时,顺带刷新抽屉头部状态
    if (activeTask.value) {
      const fresh = data.items.find((t) => t.id === activeTask.value!.id)
      if (fresh) activeTask.value = fresh
    }
  })
  if (seq === listSeq) loading.value = false
}

function search() {
  page.current = 1
  load()
}

function onTableChange(pag: { current?: number; pageSize?: number }) {
  if (pag.current) page.current = pag.current
  if (pag.pageSize) {
    page.pageSize = pag.pageSize
    page.current = 1
  }
  load()
}

let timer: number | undefined
onMounted(async () => {
  await loadSafely(async () => {
    types.value = (await fetchTaskTypes()).items
  })
  load()
  // 5s 自动刷新(开关可暂停;有运行中任务时体验良好,列表查询本身轻量)
  timer = window.setInterval(() => {
    if (autoRefresh.value && !document.hidden) load()
  }, 5000)
})
// 卸载必须连日志抽屉的 SSE 流/轮询一起停——只清列表定时器会让抽屉的
// 3s 轮询和 fetch 流在组件销毁后永久空转(登出场景),内存+网络双泄漏
onBeforeUnmount(() => {
  window.clearInterval(timer)
  stopWatching()
})

async function doCancel(record: TaskItem) {
  await loadSafely(async () => {
    const t = await cancelTask(record.id)
    message.info(`已请求取消:${statusLabel(t.status)}`)
    if (activeTask.value?.id === t.id) activeTask.value = t
  })
  load()
}

// ── 日志抽屉(SSE 实时 + 轮询降级) ──────────────────────

const logsOpen = ref(false)
const activeTask = ref<TaskItem | null>(null)
const logs = ref<TaskLogItem[]>([])
const logsBox = ref<HTMLElement | null>(null)
let abortStream: (() => void) | null = null
let pollTimer: number | undefined
let lastLogId = 0
const levelFilter = ref('')

const filteredLogs = computed(() =>
  levelFilter.value ? logs.value.filter((l) => l.level === levelFilter.value) : logs.value)
const viewMode = ref<'structured' | 'raw'>('structured')

// ── 结构化视图:按 message 前缀把日志切成事件组 ─────────────
interface LogGroup { key: string; title: string; badge?: string; logs: TaskLogItem[] }

const structuredGroups = computed<LogGroup[]>(() => {
  const groups: LogGroup[] = []
  let cur: LogGroup | null = null
  for (const lg of filteredLogs.value) {
    const g = groupOf(lg)
    if (!cur || cur.key !== g.key) { cur = { ...g, logs: [] }; groups.push(cur) }
    cur.logs.push(lg)
  }
  // 逐块组徽章 = n块·Ne/Nr;其他组取首条时间戳
  for (const g of groups) {
    if (g.key === 'chunks') {
      const ents = g.logs.reduce((s, l) => s + (Number(chunkData(l).entities) || 0), 0)
      const rels = g.logs.reduce((s, l) => s + (Number(chunkData(l).relations) || 0), 0)
      g.badge = `${g.logs.length} 块 · ${ents}e/${rels}r`
    }
  }
  return groups
})

function groupOf(lg: TaskLogItem): { key: string; title: string } {
  const m = lg.message
  if (m.startsWith('开始导入')) return { key: 'start', title: '任务启动' }
  if (m.startsWith('文档解析') || m.startsWith('结构感知切块') || m.startsWith('复用已有'))
    return { key: 'parse', title: '解析与切块' }
  if (m.startsWith('清理旧图谱') || m.startsWith('断点续跑')) return { key: 'prepare', title: '清理/续跑准备' }
  if (m.startsWith('向量模式')) return { key: 'vector', title: '向量准备' }
  if (m.startsWith('抽取配置')) return { key: 'config', title: '抽取配置' }
  if (m.startsWith('批次') && m.includes('开始')) return { key: 'batch', title: m.slice(0, m.indexOf('(')) }
  if (m.startsWith('块') && (m.includes('抽取完成') || m.includes('抽取失败')))
    return { key: 'chunks', title: '逐块抽取' }
  if (m.startsWith('批次') && m.includes('完成')) return { key: 'batchsum', title: m.slice(0, m.indexOf(':')) }
  if (m.startsWith('导入完成') || m.startsWith('样本收集') || m.startsWith('本体归纳'))
    return { key: 'summary', title: '汇总' }
  return { key: `misc-${m.slice(0, 6)}`, title: '其他' }
}

/** 逐块进度:LLM 耗时占该组最大耗时的比例(相对耗时可视化,不是完成度) */
function chunkData(lg: TaskLogItem): LogData {
  return (lg.data && typeof lg.data === 'object' ? lg.data : {}) as LogData
}

function chunkPct(lg: TaskLogItem): number {
  const d = Number(chunkData(lg).duration_ms) || 0
  const max = Math.max(...(structuredGroups.value.find((g) => g.key === 'chunks')?.logs
    .map((l) => Number(chunkData(l).duration_ms) || 1) || [1]), 1)
  return Math.max(6, Math.round((d / max) * 100))
}

function fmtMs(ms: unknown): string {
  const v = Number(ms)
  if (!v) return '-'
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`
}

/** 非 chunk 日志的关键指标徽章(挑人最关心的,不是全量) */
type LogData = Record<string, unknown>
const METRIC_KEYS: Record<string, string[]> = {
  parse: ['chunks', 'min_chars', 'max_chars', 'avg_chars', 'raw_bytes', 'text_chars'],
  config: ['batch_size', 'concurrency', 'max_retries', 'failure_threshold', 'temperature', 'todo_chunks', 'llm_model'],
  vector: ['model', 'dim'],
  batchsum: ['entities', 'relations', 'cumulative_entities', 'cumulative_relations', 'schema_dropped', 'graph_ms', 'seconds'],
  summary: ['entities', 'relations', 'chunks', 'failed_chunks', 'resumed_chunks', 'seconds'],
  batch: ['batch', 'chunks'],
}
function metricsOf(lg: TaskLogItem): { k: string; v: string }[] {
  const g = groupOf(lg)
  const keys = METRIC_KEYS[g.key]
  const d = (lg.data && typeof lg.data === 'object' ? lg.data : null) as LogData | null
  if (!keys || !d) return []
  return keys
    .filter((k) => d[k] !== undefined && d[k] !== null && d[k] !== '')
    .map((k) => ({ k, v: String(d[k]).length > 28 ? String(d[k]).slice(0, 28) + '…' : String(d[k]) }))
}

/** 结构化附加字段 → 「k=v · k=v」小字串(长数组截断,别喧宾夺主) */
function fmtData(lg: TaskLogItem): string {
  const d = lg.data
  if (!d || typeof d !== 'object') return ''
  return Object.entries(d as Record<string, unknown>)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, 8)
    .map(([k, v]) => {
      let s = String(v)
      if (Array.isArray(v)) s = v.length > 4 ? `[${v.slice(0, 4).join(',')}…×${v.length}]` : `[${v.join(',')}]`
      return `${k}=${s.length > 42 ? s.slice(0, 42) + '…' : s}`
    })
    .join(' · ')
}

// 关抽屉即停监听:SSE 流 + 轮询只为抽屉服务,抽屉关了继续跑是纯浪费
watch(logsOpen, (open) => {
  if (!open) stopWatching()
})

const FINAL = ['succeeded', 'failed', 'cancelled', 'interrupted']

function mergeLog(lg: TaskLogItem) {
  if (lg.id <= lastLogId) return
  lastLogId = lg.id
  logs.value.push(lg)
  nextTick(() => {
    if (logsBox.value) logsBox.value.scrollTop = logsBox.value.scrollHeight
  })
}

function stopWatching() {
  abortStream?.()
  abortStream = null
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

function startWatching(task: TaskItem) {
  stopWatching()
  // 终态提示纪律:只在抽屉可见且页面在前台时提示;成功用 success 静默一闪,
  // 失败/取消才用 warning 拉注意力。后台(切Tab/最小化)不弹——回来时
  // 抽屉/列表的状态本身就是结果,别打断用户正在做的事
  const notifyFinal = (status: string) => {
    if (!logsOpen.value || document.hidden) return
    if (status === 'failed' || status === 'cancelled' || status === 'interrupted') {
      message.warning(`任务${statusLabel(status as TaskStatus)}:${activeTask.value?.title?.slice(0, 40) || task.id.slice(0, 8)}`)
    } else {
      message.success(`任务${statusLabel(status as TaskStatus)}`)
    }
  }
  // SSE 主通道:快照 + 增量
  abortStream = streamTaskEvents(task.id, {
    onSnapshot: (d) => {
      activeTask.value = d.task
      lastLogId = 0
      logs.value = []
      d.logs.forEach(mergeLog)
    },
    onProgress: (d) => {
      if (activeTask.value) {
        activeTask.value.progress = d.progress
        activeTask.value.progressMessage = d.message
      }
    },
    onLog: mergeLog,
    onStatus: (d) => {
      if (activeTask.value) activeTask.value.status = d.status
      if (FINAL.includes(d.status)) {
        notifyFinal(d.status)
        stopWatching()
        load()
      }
    },
  })
  // 轮询降级:SSE 断了(或环境不支持)也能持续看到进度/日志。
  // 同时补任务状态(不只补日志)——SSE 断流期间进度条/状态会冻结,
  // 任务到终态时也靠这里停表,轮询才有自然的终点。
  // 提示去重:SSE 在途时轮询不弹(终态提示只走一条通道)
  pollTimer = window.setInterval(async () => {
    if (document.hidden) return
    try {
      const t = await fetchTask(task.id)
      if (activeTask.value?.id === task.id) {
        activeTask.value = { ...activeTask.value, ...t }
      }
      if (FINAL.includes(t.status)) {
        if (!abortStream) notifyFinal(t.status)   // SSE 已断才由轮询提示
        stopWatching()
        load()
        return
      }
      const { items } = await fetchTaskLogs(task.id, lastLogId)
      items.forEach(mergeLog)
    } catch { /* 静默:下一轮再试 */ }
  }, 3000)
}

function openLogs(record: TaskItem) {
  activeTask.value = record
  logs.value = []
  lastLogId = 0
  logsOpen.value = true
  // 先拉历史,再开实时流(流自带 snapshot,这里的历史拉取主要兜首轮延迟)
  loadSafely(async () => {
    const { items } = await fetchTaskLogs(record.id, 0)
    if (activeTask.value?.id === record.id && !logs.value.length) {
      lastLogId = 0
      items.forEach(mergeLog)
    }
  })
  startWatching(record)
}
</script>

<style scoped>
.tk-toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0 16px; }
.tk-count { margin-left: auto; color: #9ca3af; font-size: 12px; }
.tk-title { color: #374151; font-size: 13px; }
.tk-time { font-size: 12.5px; color: #4b5563; }
.tk-done { color: #16a34a; font-weight: 600; font-size: 12px; }
.tk-drawer-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.tk-msg { color: #6b7280; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tk-logs-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.tk-logs-count { color: #9ca3af; font-size: 12px; margin-left: auto; }
.tk-logs {
  background: #0f172a; border-radius: 10px; padding: 12px 14px;
  max-height: calc(100vh - 260px); overflow-y: auto; font-size: 12.5px;
}

/* ── 结构化视图 ── */
.tk-group-head {
  display: flex; align-items: center; gap: 10px; padding: 10px 0 4px;
  border-bottom: 1px solid #1e293b; margin-bottom: 4px;
}
.tk-group-head:first-child { padding-top: 0; }
.tk-group-title { font-weight: 700; font-size: 12.5px; color: #93c5fd; }
.tk-group-badge {
  font-size: 11px; color: #cbd5e1; background: #1e293b;
  border-radius: 8px; padding: 1px 8px;
}
.tk-group-time { margin-left: auto; color: #64748b; font-size: 11px; font-family: 'SF Mono', Menlo, Consolas, monospace; }
.tk-row { padding: 3px 0 3px 10px; border-left: 2px solid #1e293b; margin-left: 2px; }
.tk-row-warn { border-left-color: #f59e0b; }
.tk-row-error { border-left-color: #dc2626; }
.tk-line { display: flex; align-items: center; gap: 6px; color: #cbd5e1; }
.tk-line-msg { white-space: pre-wrap; word-break: break-all; }
.tk-metrics { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 6px; }
.tk-metric {
  display: inline-flex; align-items: center; gap: 4px;
  background: #16233b; border: 1px solid #24344f; border-radius: 6px;
  padding: 1px 7px; font-size: 11px;
}
.tk-metric-k { color: #7c8aa5; font-family: 'SF Mono', Menlo, Consolas, monospace; }
.tk-metric-v { color: #e2e8f0; font-family: 'SF Mono', Menlo, Consolas, monospace; }
.tk-chunk-row { display: flex; align-items: center; gap: 10px; padding: 2px 0; }
.tk-chunk-id {
  color: #93c5fd; font-size: 11.5px; font-weight: 600; width: 44px; flex-shrink: 0;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
}
.tk-chunk-metric { color: #cbd5e1; font-size: 11.5px; font-family: 'SF Mono', Menlo, Consolas, monospace; flex-shrink: 0; }
.tk-chunk-dur { color: #fbbf24; font-size: 11.5px; font-family: 'SF Mono', Menlo, Consolas, monospace; width: 52px; text-align: right; flex-shrink: 0; }

/* ── 原始视图 ── */
.tk-log-line { display: flex; align-items: baseline; padding: 2px 0; color: #cbd5e1; }
.tk-log-time { color: #64748b; font-family: 'SF Mono', Menlo, Consolas, monospace; margin-right: 8px; flex-shrink: 0; }
.tk-log-msg { white-space: pre-wrap; word-break: break-all; }
.tk-log-data {
  color: #7c8aa5; font-size: 11px; font-family: 'SF Mono', Menlo, Consolas, monospace;
  margin-left: 8px; opacity: 0.85;
}
.tk-log-line:has(.tk-log-level) { align-items: center; }
</style>
