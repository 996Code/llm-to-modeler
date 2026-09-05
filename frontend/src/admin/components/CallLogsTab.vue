<template>
  <div>
    <div class="cl-toolbar">
      <a-select v-model:value="filterType" style="width: 130px" @change="search">
        <a-select-option value="">全部类型</a-select-option>
        <a-select-option value="llm">LLM 调用</a-select-option>
        <a-select-option value="upstream">上游调用</a-select-option>
        <a-select-option value="graph">图谱检索</a-select-option>
        <a-select-option value="vector">向量检索</a-select-option>
      </a-select>
      <a-input v-model:value="filterConvId" placeholder="按会话 ID 过滤" style="width: 260px" allow-clear
        @pressEnter="search">
        <template #prefix><SearchOutlined style="color: #bbb" /></template>
      </a-input>
      <a-button type="primary" @click="search">查询</a-button>
      <span class="cl-count">共 {{ total }} 条</span>
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
// 调用日志:卡片化工具条 + 耗时分级着色 + JsonViewer 详情抽屉。
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { SearchOutlined } from '@ant-design/icons-vue'
import { CallLogItem, fetchCallLogs, fmtTime, shortId } from '../api'
import type { LoadSafely } from './loadSafely'
import JsonViewer from './JsonViewer.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

const rows = ref<CallLogItem[]>([])
const total = ref(0)
const loading = ref(false)
const filterType = ref('')
const filterConvId = ref('')

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
// 检索类型过滤时插入"召回"列(命中量/匹配度一眼可见)
const columns = computed(() => {
  if (filterType.value !== 'graph' && filterType.value !== 'vector') return baseColumns
  return [...baseColumns.slice(0, 5), { title: '召回 / 匹配度', key: 'recall', width: 150 }, ...baseColumns.slice(5)]
})

const detail = ref<CallLogItem | null>(null)
const detailOpen = ref(false)

/** 调用类型元数据:标签 + 颜色(graph/vector 是知识图谱检索两路) */
const TYPE_META: Record<string, { label: string; color: string }> = {
  llm: { label: 'LLM', color: 'purple' },
  upstream: { label: '上游', color: 'cyan' },
  graph: { label: '图谱', color: 'geekblue' },
  vector: { label: '向量', color: 'green' },
}

/** kg.* 环节中文名(与链路视图一致) */
const STAGE_LABELS: Record<string, string> = {
  'kg.query': 'LLM·检索意图解析',
  'kg.query_embed': 'LLM·查询向量化',
  'kg.answer': 'LLM·组织回答',
  'kg.find_entities': '图谱·种子实体匹配',
  'kg.subgraph': '图谱·子图召回',
  'kg.vector_search': '向量·相似检索',
}

/** 地址列:LLM/检索显示中文环节名,上游显示接口路径(截掉域名前缀) */
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

/** 检索调用的召回摘要:命中数 + 匹配度(图谱=召回节点/边,向量=top 相似度) */
function recallLabel(r: CallLogItem): string {
  const resp = r.response_data as
    | { hits?: number; nodes?: number; edges?: number; topScore?: number | null } | null
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
    })
    rows.value = data.items
    total.value = data.total
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

function openDetail(record: CallLogItem) {
  detail.value = record
  detailOpen.value = true
}

onMounted(load)
</script>

<style scoped>
.cl-toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0 16px; }
.cl-count { margin-left: auto; color: #9ca3af; font-size: 12px; }
.cl-type { font-weight: 600; }
.cl-endpoint { color: #374151; font-size: 13px; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; }
.cl-dur { font-weight: 600; color: #374151; }
.dur-orange { color: #ea580c; }
.dur-red { color: #dc2626; }
.cl-recall { font-size: 12.5px; color: #2563eb; font-variant-numeric: tabular-nums; }
.cl-conv { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; color: #6b7280; }
.cl-time { font-size: 12.5px; color: #4b5563; }
</style>
