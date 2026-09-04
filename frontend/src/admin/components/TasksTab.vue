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
        <div class="tk-logs" ref="logsBox">
          <div v-for="lg in logs" :key="lg.id" class="tk-log-line">
            <span class="tk-log-time">{{ fmtTime(lg.createdAt).slice(11) }}</span>
            <a-tag
              class="tk-log-level" :color="lg.level === 'error' ? 'red' : lg.level === 'warn' ? 'orange' : 'blue'"
              style="margin-inline-end: 6px"
            >{{ lg.level }}</a-tag>
            <span class="tk-log-msg">{{ lg.message }}</span>
          </div>
          <a-empty v-if="!logs.length" description="暂无日志" style="padding: 40px 0" />
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 任务中心:任务表格(进度条/状态) + 日志抽屉(SSE 实时 + 断流降级轮询)。
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
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

async function load() {
  loading.value = true
  await loadSafely(async () => {
    const data = await fetchTasks({
      limit: page.pageSize,
      offset: (page.current - 1) * page.pageSize,
      status: filterStatus.value || undefined,
      type: filterType.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
    // 抽屉打开且当前任务在列表里时,顺带刷新抽屉头部状态
    if (activeTask.value) {
      const fresh = data.items.find((t) => t.id === activeTask.value!.id)
      if (fresh) activeTask.value = fresh
    }
  })
  loading.value = false
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
        message.info(`任务${statusLabel(d.status)}`)
        stopWatching()
        load()
      }
    },
  })
  // 轮询降级:SSE 断了(或环境不支持)也能持续看到进度/日志。
  // 同时补任务状态(不只补日志)——SSE 断流期间进度条/状态会冻结,
  // 任务到终态时也靠这里停表,轮询才有自然的终点
  pollTimer = window.setInterval(async () => {
    if (document.hidden) return
    try {
      const t = await fetchTask(task.id)
      if (activeTask.value?.id === task.id) {
        activeTask.value = { ...activeTask.value, ...t }
      }
      if (FINAL.includes(t.status)) {
        message.info(`任务${statusLabel(t.status)}`)
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
.tk-logs {
  background: #0f172a; border-radius: 10px; padding: 12px 14px;
  max-height: calc(100vh - 220px); overflow-y: auto; font-size: 12.5px;
}
.tk-log-line { display: flex; align-items: baseline; padding: 2px 0; color: #cbd5e1; }
.tk-log-time { color: #64748b; font-family: 'SF Mono', Menlo, Consolas, monospace; margin-right: 8px; flex-shrink: 0; }
.tk-log-msg { white-space: pre-wrap; word-break: break-all; }
.tk-log-line:has(.tk-log-level) { align-items: center; }
</style>
