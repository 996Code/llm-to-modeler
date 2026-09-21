<template>
  <div>
    <div class="cl-toolbar">
      <a-radio-group v-model:value="viewMode" size="small" style="margin-right:12px">
        <a-radio-button value="detail">调用明细</a-radio-button>
        <a-radio-button value="stats">按环节统计</a-radio-button>
        <a-radio-button value="audit">审计日志</a-radio-button>
      </a-radio-group>

      <!-- 调用明细筛选 -->
      <template v-if="viewMode === 'detail'">
        <a-select v-model:value="filterType" style="width: 130px" @change="search">
          <a-select-option value="">全部类型</a-select-option>
          <a-select-option value="llm">LLM 调用</a-select-option>
          <a-select-option value="upstream">上游调用</a-select-option>
          <a-select-option value="graph">图谱检索</a-select-option>
          <a-select-option value="vector">向量检索</a-select-option>
        </a-select>
        <a-select v-model:value="filterPack" style="width: 150px" placeholder="按插件过滤" allow-clear
          @change="search">
          <a-select-option v-for="p in packOptions" :key="p.value" :value="p.value">{{ p.label }}</a-select-option>
        </a-select>
        <a-input v-model:value="filterConvId" placeholder="按会话 ID 过滤" style="width: 260px" allow-clear
          @pressEnter="search">
          <template #prefix><SearchOutlined style="color: #bbb" /></template>
        </a-input>
        <a-button type="primary" @click="search">查询</a-button>
        <span class="cl-count">共 {{ total }} 条</span>
      </template>

      <!-- 按环节统计 -->
      <template v-else-if="viewMode === 'stats'">
        <a-select v-model:value="statsFilterPack" style="width: 150px" placeholder="按插件过滤" allow-clear
          @change="loadStats">
          <a-select-option v-for="p in packOptions" :key="p.value" :value="p.value">{{ p.label }}</a-select-option>
        </a-select>
        <span class="cl-count">合计 {{ fmtTokens(statsTotalTokens) }} token</span>
        <a-button size="small" @click="loadStats"><ReloadOutlined /> 刷新</a-button>
      </template>

      <!-- 审计日志筛选 -->
      <template v-else>
        <a-select v-model:value="auditFilterType" style="width: 120px" placeholder="资源类型" allow-clear @change="loadAudit">
          <a-select-option value="datasource">数据源</a-select-option>
          <a-select-option value="semantic">语义层</a-select-option>
          <a-select-option value="dashboard">看板</a-select-option>
          <a-select-option value="memory">记忆</a-select-option>
          <a-select-option value="conversation">会话</a-select-option>
          <a-select-option value="skill">规则</a-select-option>
        </a-select>
        <a-select v-model:value="auditFilterAction" style="width: 100px" placeholder="动作" allow-clear @change="loadAudit">
          <a-select-option value="create">创建</a-select-option>
          <a-select-option value="update">更新</a-select-option>
          <a-select-option value="delete">删除</a-select-option>
          <a-select-option value="scan">扫描</a-select-option>
          <a-select-option value="rollback">回滚</a-select-option>
          <a-select-option value="consolidate">整理</a-select-option>
          <a-select-option value="chat">对话</a-select-option>
          <a-select-option value="login">登录</a-select-option>
        </a-select>
        <a-input v-model:value="auditFilterUser" placeholder="按用户过滤" style="width: 150px" allow-clear @pressEnter="loadAudit" />
        <a-button type="primary" @click="loadAudit">查询</a-button>
        <span class="cl-count">共 {{ auditTotal }} 条</span>
      </template>
    </div>

    <!-- 调用明细表格 -->
    <template v-if="viewMode === 'detail'">
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
        <template v-if="column.key === 'call_type'">
          <a-tag :color="TYPE_META[record.call_type]?.color || 'default'" class="cl-type">
            {{ TYPE_META[record.call_type]?.label || record.call_type }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'endpoint'">
          <div class="cl-endpoint" :title="record.endpoint">{{ endpointLabel(record) }}</div>
        </template>
        <template v-else-if="column.key === 'status_code'">
          <a-tag v-if="record.status_code != null"
                :color="record.status_code < 400 ? 'green' : 'red'">{{ record.status_code }}</a-tag>
          <span v-else>-</span>
        </template>
        <template v-else-if="column.key === 'duration_ms'">
          <span class="cl-dur" :class="durClass(record.duration_ms)">{{ fmtDur(record.duration_ms) }}</span>
        </template>
        <template v-else-if="column.key === 'recall'">
          <span class="cl-recall">{{ recallLabel(record) || '-' }}</span>
        </template>
        <template v-else-if="column.key === 'conv_id'">
          <a-tooltip :title="record.conv_id"><span class="cl-conv">{{ shortId(record.conv_id) }}</span></a-tooltip>
        </template>
        <template v-else-if="column.key === 'created_at'">
          <span class="cl-time">{{ fmtTime(record.created_at) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a @click="openDetail(record)">详情</a>
        </template>
      </template>
    </a-table>
    </template>

    <!-- 按环节统计表格 -->
    <template v-else-if="viewMode === 'stats'">
      <a-table :columns="statsColumns" :data-source="statsRows" :loading="statsLoading"
               row-key="stage" size="middle" :pagination="false">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'stage'">
            <a-tag :color="STAGE_LABELS[record.stage] ? 'purple' : 'default'">
              {{ STAGE_LABELS[record.stage] || record.stage }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'callCount'">
            <b>{{ record.callCount }}</b>
          </template>
          <template v-else-if="column.key === 'tokens'">
            <span class="cl-tokens">{{ fmtTokens(record.promptTokens + record.completionTokens) }}</span>
            <span class="cl-tokens-sub">(入 {{ fmtTokens(record.promptTokens) }} / 出 {{ fmtTokens(record.completionTokens) }})</span>
          </template>
        </template>
      </a-table>
    </template>

    <!-- 审计日志表格 -->
    <template v-else>
      <a-table :columns="auditColumns" :data-source="auditRows" :loading="auditLoading"
               :pagination="auditPagination" row-key="id" size="middle" @change="onAuditTableChange">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'created_at'">
            <span class="cl-time">{{ fmtTime(record.created_at) }}</span>
          </template>
          <template v-else-if="column.key === 'user_id'">
            <a-tooltip :title="record.user_id">{{ shortId(record.user_id) }}</a-tooltip>
          </template>
          <template v-else-if="column.key === 'resource_type'">
            <a-tag>{{ AUDIT_TYPE_LABELS[record.resource_type] || record.resource_type }}</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <span>{{ AUDIT_ACTION_LABELS[record.action] || record.action }}</span>
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag :color="record.status === 'success' ? 'green' : record.status === 'fail' ? 'red' : 'orange'">
              {{ record.status === 'success' ? '成功' : record.status === 'fail' ? '失败' : record.status === 'denied' ? '拒绝' : record.status }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'detail'">
            <a-popover v-if="record.detail" trigger="click" placement="leftTop"
                       overlay-class-name="audit-detail-popover">
              <span class="cl-detail-summary cl-detail-click">
                {{ auditDetailSummary(record) || '(查看)' }}
              </span>
              <template #content>
                <pre class="audit-detail-full">{{ JSON.stringify(record.detail, null, 2) }}</pre>
              </template>
            </a-popover>
            <span v-else class="cl-detail-summary">-</span>
          </template>
          <template v-else-if="column.key === 'conv_id'">
            <a-tooltip v-if="record.conv_id" :title="record.conv_id">
              <span class="cl-conv">{{ shortId(record.conv_id) }}</span>
            </a-tooltip>
            <a-tooltip v-else title="数据源/语义层等管理操作无关联会话">
              <span class="cl-conv-none">管理操作</span>
            </a-tooltip>
          </template>
        </template>
      </a-table>
    </template>

    <!-- 调用详情抽屉 -->
    <a-drawer v-model:open="detailOpen" width="680" :title="detail?.endpoint || '调用详情'">
      <template v-if="detail">
        <a-descriptions :column="2" size="small" bordered style="margin-bottom: 14px">
          <a-descriptions-item label="类型">
            <a-tag :color="TYPE_META[detail.call_type]?.color || 'default'">
              {{ TYPE_META[detail.call_type]?.label || detail.call_type }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="状态码">{{ detail.status_code ?? '-' }}</a-descriptions-item>
          <a-descriptions-item label="耗时">{{ detail.duration_ms ?? '-' }} ms</a-descriptions-item>
          <a-descriptions-item label="会话">{{ detail.conv_id || '-' }}</a-descriptions-item>
          <a-descriptions-item label="时间" :span="2">{{ fmtTime(detail.created_at) }}</a-descriptions-item>
          <a-descriptions-item v-if="detail.error_message" label="错误" :span="2">
            <span style="color: #dc2626">{{ detail.error_message }}</span>
          </a-descriptions-item>
        </a-descriptions>
        <JsonViewer label="请求" :data="detail.request_data" :height="280" :default-collapsed="false" />
        <JsonViewer label="响应" :data="detail.response_data" :height="320" />
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 审计 & 调用:三视角 Tab(调用明细/按环节统计/审计日志)。
import { computed, inject, onMounted, reactive, ref, watch } from 'vue'
import { SearchOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { CallLogItem, fetchCallLogs, fetchCallStats, fetchAuditLogs, fmtTime, shortId, CallStageItem, AuditEventItem, fetchPacks } from '../api'
import type { LoadSafely } from './loadSafely'
import JsonViewer from './JsonViewer.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

const rows = ref<CallLogItem[]>([])
const total = ref(0)
const loading = ref(false)
const filterType = ref('')
const filterConvId = ref('')
const filterPack = ref('')
const packOptions = ref<{ value: string; label: string }[]>([])

const viewMode = ref<'detail' | 'stats' | 'audit'>('detail')
const statsRows = ref<CallStageItem[]>([])
const statsLoading = ref(false)
const statsTotalTokens = ref(0)
const statsFilterPack = ref('')
const statsColumns = [
  { title: '环节', key: 'stage', width: 220 },
  { title: '调用次数', key: 'callCount', width: 100 },
  { title: 'Token 用量', key: 'tokens' },
]

// 审计日志
const auditRows = ref<AuditEventItem[]>([])
const auditTotal = ref(0)
const auditLoading = ref(false)
const auditFilterType = ref('')
const auditFilterAction = ref('')
const auditFilterUser = ref('')
const auditPage = reactive({ current: 1, pageSize: 20 })
const auditPagination = computed(() => ({
  total: auditTotal.value,
  current: auditPage.current,
  pageSize: auditPage.pageSize,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50', '100'],
  showTotal: (t: number) => `共 ${t} 条`,
}))
const auditColumns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '用户', key: 'user_id', width: 100, ellipsis: true },
  { title: '资源类型', key: 'resource_type', width: 100 },
  { title: '动作', key: 'action', width: 90 },
  { title: '结果', key: 'status', width: 73 },
  { title: '详情', key: 'detail', ellipsis: true },
  { title: '关联会话', key: 'conv_id', width: 110 },
]

const AUDIT_TYPE_LABELS: Record<string, string> = {
  datasource: '数据源', semantic: '语义层', dashboard: '看板',
  memory: '记忆', conversation: '会话', skill: '规则',
}
const AUDIT_ACTION_LABELS: Record<string, string> = {
  create: '创建', update: '更新', delete: '删除', scan: '扫描',
  rollback: '回滚', consolidate: '整理', chat: '对话', login: '登录',
}

function auditDetailSummary(r: AuditEventItem): string {
  const d = r.detail as Record<string, unknown> | null
  if (!d) return ''
  const parts: string[] = []
  if (d.name) parts.push(`「${d.name}」`)
  if (d.from_version != null && d.to_version != null) parts.push(`v${d.from_version}→v${d.to_version}`)
  if (d.error) parts.push(`错误: ${d.error}`)
  return parts.join(' · ') || JSON.stringify(d).slice(0, 80)
}

const page = reactive({ current: 1, pageSize: 20 })
const pagination = computed(() => ({
  total: total.value,
  current: page.current,
  pageSize: page.pageSize,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50', '100'],
  showTotal: (t: number) => `共 ${t} 条`,
}))

const baseColumns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '类型', key: 'call_type', width: 82 },
  { title: '地址 / 环节', key: 'endpoint', ellipsis: true },
  { title: '状态', key: 'status_code', width: 76 },
  { title: '耗时', key: 'duration_ms', width: 100 },
  { title: '会话', key: 'conv_id', width: 105 },
  { title: '操作', key: 'actions', width: 76 },
]
const columns = computed(() => {
  if (filterType.value !== 'graph' && filterType.value !== 'vector') return baseColumns
  return [...baseColumns.slice(0, 5), { title: '召回 / 匹配度', key: 'recall', width: 150 }, ...baseColumns.slice(5)]
})

const detail = ref<CallLogItem | null>(null)
const detailOpen = ref(false)

import { CALL_TYPE_META as TYPE_META, STAGE_LABELS } from '../labels'

function endpointLabel(r: CallLogItem): string {
  const req = r.request_data as { stage?: string } | null
  const stage = req?.stage
  if (r.call_type === 'llm') {
    if (stage && STAGE_LABELS[stage]) return STAGE_LABELS[stage]
    if (stage) return `[${stage}] chat/completions`
    return 'chat/completions'
  }
  if (r.call_type === 'graph' || r.call_type === 'vector') {
    const op = String(r.endpoint || '').split(':').pop()
    if (stage && STAGE_LABELS[stage]) return `${STAGE_LABELS[stage]}(${op})`
    return op || '-'
  }
  const m = String(r.endpoint || '').match(/https?:\/\/[^/]+(.*)/)
  return (m ? m[1] : r.endpoint) || '-'
}

function recallLabel(r: CallLogItem): string {
  const resp = r.response_data as { hits?: number; nodes?: number; edges?: number; topScore?: number | null } | null
  if (!resp) return ''
  if (r.call_type === 'vector') {
    if (resp.hits == null) return ''
    const score = resp.topScore != null ? ` · top ${resp.topScore}` : ''
    return `${resp.hits} 命中${score}`
  }
  if (r.call_type === 'graph') {
    if (resp.nodes == null && resp.hits == null) return ''
    if (resp.hits != null) return `${resp.hits} 种子`
    return `${resp.nodes ?? 0} 节点 / ${resp.edges ?? 0} 边`
  }
  return ''
}

function durClass(ms: number | null | undefined): string[] {
  if (ms == null) return []
  if (ms >= 30000) return ['dur-red']
  if (ms >= 3000) return ['dur-orange']
  return []
}

function fmtDur(ms: number | null | undefined): string {
  if (ms == null) return '-'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

async function load() {
  loading.value = true
  await loadSafely(async () => {
    const data = await fetchCallLogs({
      limit: page.pageSize,
      offset: (page.current - 1) * page.pageSize,
      callType: filterType.value || undefined,
      convId: filterConvId.value.trim() || undefined,
      packName: filterPack.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
  })
  loading.value = false
}

function search() { page.current = 1; load() }

function onTableChange(pag: { current?: number; pageSize?: number }) {
  if (pag.current) page.current = pag.current
  if (pag.pageSize) { page.pageSize = pag.pageSize; page.current = 1 }
  load()
}

function openDetail(record: CallLogItem) { detail.value = record; detailOpen.value = true }

onMounted(() => { load(); loadStats(); loadAudit(); loadPackOptions() })

async function loadPackOptions() {
  try {
    const data = await fetchPacks()
    packOptions.value = data.items.map((p) => ({ value: p.name, label: p.name }))
  } catch { /* 静默 */ }
}

watch(viewMode, (v) => {
  if (v === 'stats') loadStats()
  else if (v === 'audit') loadAudit()
})

function fmtTokens(n?: number): string {
  if (!n) return '0'
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

async function loadStats() {
  statsLoading.value = true
  try {
    const data = await fetchCallStats(statsFilterPack.value || undefined)
    statsRows.value = data.items
    statsTotalTokens.value = data.totalTokens
  } catch { statsRows.value = [] }
  statsLoading.value = false
}

async function loadAudit() {
  auditLoading.value = true
  try {
    const data = await fetchAuditLogs({
      limit: auditPage.pageSize,
      offset: (auditPage.current - 1) * auditPage.pageSize,
      resourceType: auditFilterType.value || undefined,
      action: auditFilterAction.value || undefined,
      userId: auditFilterUser.value.trim() || undefined,
    })
    auditRows.value = data.items
    auditTotal.value = data.total
  } catch { auditRows.value = [] }
  auditLoading.value = false
}

function onAuditTableChange(pag: { current?: number; pageSize?: number }) {
  if (pag.current) auditPage.current = pag.current
  if (pag.pageSize) { auditPage.pageSize = pag.pageSize; auditPage.current = 1 }
  loadAudit()
}
</script>

<style scoped>
.cl-toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0 16px; flex-wrap: wrap; }
.cl-count { margin-left: auto; color: #9ca3af; font-size: 12px; }
.cl-type { font-weight: 600; }
.cl-endpoint { color: #374151; font-size: 13px; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; }
.cl-dur { font-weight: 600; color: #374151; }
.dur-orange { color: #ea580c; }
.dur-red { color: #dc2626; }
.cl-recall { font-size: 12.5px; color: #2563eb; font-variant-numeric: tabular-nums; }
.cl-conv { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; color: #6b7280; }
.cl-time { font-size: 12.5px; color: #4b5563; }
.cl-tokens { font-weight: 600; color: #374151; font-variant-numeric: tabular-nums; }
.cl-tokens-sub { font-size: 11.5px; color: #9ca3af; margin-left: 6px; }
.cl-detail-summary { font-size: 12px; color: #4b5563; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 280px; display: inline-block; }

.cl-detail-click { cursor: pointer; text-decoration: underline dotted; text-underline-offset: 3px; }
.cl-detail-click:hover { color: #1677ff; }
.cl-conv-none { color: #bbb; font-size: 12px; }
</style>
<style>
.audit-detail-popover .audit-detail-full {
  max-width: 520px; max-height: 340px; overflow: auto; margin: 0;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all;
  background: #f7f8fa; padding: 10px 12px; border-radius: 6px;
}
</style>
