<template>
  <div class="sq-page">
    <a-card class="section-card">
      <template #title>
        <HistoryOutlined /> 保存查询
        <span class="muted">{{ queries.length }} 条</span>
      </template>
      <template #extra>
        <a-button size="small" @click="loadQueries"><ReloadOutlined /> 刷新</a-button>
      </template>
      <a-table :data-source="queries" :loading="loadingQ" row-key="id" size="small"
               :pagination="queries.length > 20 ? { pageSize: 20 } : false">
        <a-table-column title="问题" data-index="question" :ellipsis="true" />
        <a-table-column title="SQL" width="320">
          <template #default="{ record }">
            <a-tooltip :title="record.sqlText" placement="topLeft">
              <code class="sql-cell">{{ record.sqlText }}</code>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="结果" data-index="rowCount" width="80">
          <template #default="{ record }">{{ record.resultSummary?.row_count ?? '-' }} 行</template>
        </a-table-column>
        <a-table-column title="时间" data-index="createdAt" width="160">
          <template #default="{ record }">{{ fmtTime(record.createdAt) }}</template>
        </a-table-column>
        <a-table-column title="操作" width="230">
          <template #default="{ record }">
            <a-button size="small" type="link" @click="exportCsv(record)">
              <DownloadOutlined /> 导出 CSV
            </a-button>
            <a-button size="small" type="link" @click="openAddToDash(record)">
              <AppstoreAddOutlined /> 加到看板
            </a-button>
          </template>
        </a-table-column>
      </a-table>
      <a-empty v-if="!loadingQ && !queries.length" description="暂无保存查询——在对话中完成一次查询即自动保存" />
    </a-card>

    <!-- 加到看板(双栏) -->
    <a-modal v-model:open="showAddWidget" title="添加到看板" ok-text="添加" cancel-text="取消"
             @ok="addWidget">
      <a-form layout="vertical" class="aw-form">
        <div class="form-row">
          <a-form-item label="目标看板" required class="half">
            <a-select v-model:value="addWidgetForm.dashboardId" placeholder="选择看板">
              <a-select-option v-for="d in dashboards" :key="d.id" :value="d.id">{{ d.name }}</a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item label="图表类型" class="half">
            <a-select v-model:value="addWidgetForm.chartType">
              <a-select-option value="auto">自动</a-select-option>
              <a-select-option value="bar">柱状图</a-select-option>
              <a-select-option value="pie">饼图</a-select-option>
              <a-select-option value="line">折线图</a-select-option>
              <a-select-option value="table">表格</a-select-option>
            </a-select>
          </a-form-item>
        </div>
        <div class="aw-preview">
          <div class="aw-preview-label">将添加的查询</div>
          <div class="aw-preview-q">{{ pendingQuery?.question }}</div>
        </div>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 保存查询子页:查询列表/CSV 导出/加到看板(emit 给父级联动看板 Tab)。
// 从原 m4.vue 拆出。
import { defineEmits, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  AppstoreAddOutlined, DownloadOutlined, HistoryOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'

const emit = defineEmits<{ (e: 'add-to-dash', payload: { dashboardId?: string }): void }>()

function uid(): string {
  return localStorage.getItem('chatbi_user_id') || 'admin'
}
function fmtTime(iso: string): string {
  return iso ? iso.replace('T', ' ').slice(0, 16) : ''
}

const queries = ref<any[]>([])
const loadingQ = ref(false)
const dashboards = ref<any[]>([])
const showAddWidget = ref(false)
const addWidgetForm = reactive({ dashboardId: '', chartType: 'auto' })
let pendingQuery: any = null

async function loadQueries() {
  loadingQ.value = true
  try {
    const { data } = await chatbiApi.get('/saved-queries',
                                         { params: { limit: 100 }, headers: { 'X-User-Id': uid() } })
    queries.value = data.items || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  } finally { loadingQ.value = false }
}

async function loadDashboards() {
  try {
    const { data } = await chatbiApi.get('/dashboards', { headers: { 'X-User-Id': uid() } })
    dashboards.value = data.items || []
  } catch { /* 看板列表失败不阻塞查询页 */ }
}

async function exportCsv(record: any) {
  try {
    const resp = await fetch(
      `${chatbiApi.defaults.baseURL}/saved-queries/${record.id}/export`,
      { headers: { 'X-Admin-Token': localStorage.getItem('admin_token') || '',
                   'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}`,
                   'X-User-Id': uid() } })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `query-${record.id.slice(0, 8)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    message.error(`导出失败: ${e.message}`)
  }
}

function openAddToDash(record: any) {
  pendingQuery = record
  addWidgetForm.dashboardId = dashboards.value[0]?.id || ''
  addWidgetForm.chartType = 'auto'
  if (!dashboards.value.length) { message.info('请先在看板页创建一个看板'); return }
  showAddWidget.value = true
}

async function addWidget() {
  if (!addWidgetForm.dashboardId) { message.warning('请选择看板'); return }
  try {
    await chatbiApi.post(`/dashboards/${addWidgetForm.dashboardId}/widgets`, {
      question: pendingQuery.question,
      query_sql: pendingQuery.sqlText,
      datasource_id: pendingQuery.dataSourceId,
      chart_type: addWidgetForm.chartType === 'auto' ? 'bar' : addWidgetForm.chartType,
    }, { headers: { 'X-User-Id': uid() } })
    message.success('已添加到看板')
    showAddWidget.value = false
    emit('add-to-dash', { dashboardId: addWidgetForm.dashboardId })
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '添加失败')
  }
}

onMounted(() => { loadQueries(); loadDashboards() })
</script>

<style scoped>
.sq-page { display: flex; flex-direction: column; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
.sql-cell {
  display: inline-block; max-width: 300px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; background: #f5f5f5; padding: 1px 6px; border-radius: 3px;
  font-size: 12px; vertical-align: middle;
}
.aw-form .form-row { display: flex; gap: 12px; }
.aw-form .half { flex: 1; }
.aw-preview {
  background: #f7f8fa; border-radius: 8px; padding: 10px 12px; margin-top: 4px;
}
.aw-preview-label { font-size: 12px; color: #86909c; margin-bottom: 4px; }
.aw-preview-q { font-size: 13px; color: #1f2329; font-weight: 500; }
</style>
