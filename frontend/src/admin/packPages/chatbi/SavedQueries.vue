<template>
  <div class="sq-page">
    <div class="tab-toolbar">
      <span>共 <b>{{ queries.length }}</b> 条</span>
      <a-button size="small" @click="loadQueries"><ReloadOutlined /> 刷新</a-button>
    </div>
    <div class="sq-hint">
      <InfoCircleOutlined /> 行数为保存查询那一刻的快照;导出 Excel 与加入看板时会<b>实时重跑 SQL</b> 取最新数据
    </div>
    <a-table :data-source="queries" :loading="loadingQ" row-key="id" size="small"
             :pagination="queries.length > 20 ? { pageSize: 20 } : false">
        <a-table-column title="问题" data-index="question" :ellipsis="true" />
        <a-table-column title="SQL" :ellipsis="true" width="300">
          <template #default="{ record }">
            <a-popover trigger="click" placement="leftTop"
                       overlay-class-name="sql-popover">
              <code class="sql-cell">{{ record.sqlText }}</code>
              <template #content>
                <pre class="sql-full">{{ record.sqlText }}</pre>
              </template>
            </a-popover>
          </template>
        </a-table-column>
        <a-table-column width="80">
          <template #title>
            <span>结果
              <a-tooltip title="保存查询那一刻的行数快照——导出/看板会实时重跑 SQL, 行数可能不同">
                <InfoCircleOutlined style="color:#bbb;cursor:help;margin-left:2px" />
              </a-tooltip>
            </span>
          </template>
          <template #default="{ record }">{{ record.resultSummary?.row_count ?? '-' }} 行</template>
        </a-table-column>
        <a-table-column title="时间" data-index="createdAt" width="160">
          <template #default="{ record }">{{ fmtTime(record.createdAt) }}</template>
        </a-table-column>
        <a-table-column title="操作" width="230">
          <template #default="{ record }">
            <a-button size="small" type="link" @click="exportExcel(record)">
              <DownloadOutlined /> 导出 Excel
            </a-button>
            <a-button size="small" type="link" @click="openAddToDash(record)">
              <AppstoreAddOutlined /> 加到看板
            </a-button>
          </template>
        </a-table-column>
      </a-table>
      <a-empty v-if="!loadingQ && !queries.length" description="暂无保存查询——在对话中完成一次查询即自动保存" />

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
  AppstoreAddOutlined, DownloadOutlined, HistoryOutlined, InfoCircleOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'
import { exportQueryToExcel } from '../../../utils/exportExcel'

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

async function exportExcel(record: any) {
  // 重跑 SQL 取实时数据 → 前端 exceljs 生成 .xlsx(数据 sheet, 原版同款体验)
  try {
    const resp = await fetch(
      `${chatbiApi.defaults.baseURL}/saved-queries/${record.id}/run`,
      { headers: { 'X-Admin-Token': localStorage.getItem('admin_token') || '',
                   'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}`,
                   'X-User-Id': uid() } })
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}))
      throw new Error(d.detail || `HTTP ${resp.status}`)
    }
    const data = await resp.json()
    if (!data.columns?.length) { message.warning('该查询无数据可导出'); return }
    await exportQueryToExcel({
      question: data.question || record.question,
      columns: data.columns,
      rows: data.rows || [],
    })
    message.success('已导出 Excel')
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
.tab-toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 12px; font-size: 13px; color: #86909c;
}
.tab-toolbar b { color: #1d2129; }
.sq-hint { font-size: 12px; color: #999; margin-bottom: 12px; }
.sq-hint b { color: #4e5969; font-weight: 500; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
/* 列宽内单行省略(ellipsis 由 a-table-column 接管, 不再 max-width 硬限——
   此前 inline-block 300px 在窄屏下溢出覆盖右侧"结果"列) */
.sql-cell {
  display: block; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; background: #f5f5f5; padding: 2px 6px; border-radius: 3px;
  font-size: 12px; cursor: pointer;
}
.sql-cell:hover { background: #eef1f6; }
.aw-form .form-row { display: flex; gap: 12px; }
.aw-form .half { flex: 1; }
.aw-preview {
  background: #f7f8fa; border-radius: 8px; padding: 10px 12px; margin-top: 4px;
}
.aw-preview-label { font-size: 12px; color: #86909c; margin-bottom: 4px; }
.aw-preview-q { font-size: 13px; color: #1f2329; font-weight: 500; }
</style>

<style>
.sql-popover .sql-full {
  max-width: 560px; max-height: 320px; overflow: auto; margin: 0;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all;
  background: #1e1e1e; color: #a5d6ff; padding: 10px 12px; border-radius: 6px;
}
</style>
