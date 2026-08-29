<template>
  <div>
    <div class="cl-toolbar">
      <a-select v-model:value="filterType" style="width: 130px" @change="search">
        <a-select-option value="">全部类型</a-select-option>
        <a-select-option value="llm">LLM 调用</a-select-option>
        <a-select-option value="upstream">上游调用</a-select-option>
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
          <a-tag :color="record.call_type === 'llm' ? 'purple' : 'cyan'" class="cl-type">
            {{ record.call_type === 'llm' ? 'LLM' : '上游' }}
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
            <a-tag :color="detail.call_type === 'llm' ? 'purple' : 'cyan'">{{ detail.call_type }}</a-tag>
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

const columns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '类型', key: 'call_type', width: 82 },
  { title: '地址 / 环节', key: 'endpoint', ellipsis: true },
  { title: '状态', key: 'status_code', width: 76 },
  { title: '耗时', key: 'duration_ms', width: 100 },
  { title: '会话', key: 'conv_id', width: 105 },
  { title: '操作', key: 'actions', width: 76 },
]

const detail = ref<CallLogItem | null>(null)
const detailOpen = ref(false)

/** 地址列:LLM 显示环节(stage),上游显示接口路径(截掉域名前缀) */
function endpointLabel(r: CallLogItem): string {
  const req = r.request_data as { stage?: string } | null
  if (r.call_type === 'llm' && req?.stage) return `[${req.stage}] chat/completions`
  const m = String(r.endpoint || '').match(/https?:\/\/[^/]+(.*)/)
  return (m ? m[1] : r.endpoint) || '-'
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
.cl-conv { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; color: #6b7280; }
.cl-time { font-size: 12.5px; color: #4b5563; }
</style>
