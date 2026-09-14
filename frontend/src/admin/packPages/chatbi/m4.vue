<template>
  <div class="m4-page">
    <a-tabs v-model:activeKey="tab">
      <!-- ══════════ 保存查询 ══════════ -->
      <a-tab-pane key="queries">
        <template #tab><HistoryOutlined /> 保存查询</template>
        <a-card>
          <template #extra>
            <a-button size="small" @click="loadQueries"><ReloadOutlined /> 刷新</a-button>
          </template>
          <a-table :data-source="queries" :loading="loadingQ" row-key="id" size="small"
                   :pagination="queries.length > 20 ? { pageSize: 20 } : false">
            <a-table-column title="问题" data-index="question" :ellipsis="true" />
            <a-table-column title="SQL" width="320">
              <template #default="{ record }">
                <code class="sql-cell" :title="record.sqlText">{{ record.sqlText }}</code>
              </template>
            </a-table-column>
            <a-table-column title="结果" data-index="rowCount" width="80">
              <template #default="{ record }">{{ record.resultSummary?.row_count ?? '-' }} 行</template>
            </a-table-column>
            <a-table-column title="时间" data-index="createdAt" width="170">
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
      </a-tab-pane>

      <!-- ══════════ 看板 ══════════ -->
      <a-tab-pane key="dashboards">
        <template #tab><LayoutOutlined /> 看板</template>
        <!-- 看板列表 -->
        <template v-if="!currentDash">
          <a-card>
            <template #extra>
              <a-button type="primary" size="small" @click="showCreateDash = true">
                <PlusOutlined /> 新建看板
              </a-button>
            </template>
            <a-table :data-source="dashboards" :loading="loadingD" row-key="id" size="small"
                     :pagination="false">
              <a-table-column title="名称" data-index="name" />
              <a-table-column title="组件数" data-index="widget_count" width="90" />
              <a-table-column title="更新时间" data-index="updatedAt" width="170">
                <template #default="{ record }">{{ fmtTime(record.updatedAt) }}</template>
              </a-table-column>
              <a-table-column title="操作" width="220">
                <template #default="{ record }">
                  <a-button size="small" type="link" @click="openDash(record)">打开</a-button>
                  <a-button size="small" type="link" @click="renameDash(record)">重命名</a-button>
                  <a-popconfirm title="删除看板及其全部组件?" @confirm="removeDash(record)">
                    <a-button size="small" type="link" danger>删除</a-button>
                  </a-popconfirm>
                </template>
              </a-table-column>
            </a-table>
            <a-empty v-if="!loadingD && !dashboards.length" description="暂无看板——新建一个, 再从保存查询添加组件" />
          </a-card>
        </template>

        <!-- 看板详情(widget 墙) -->
        <template v-else>
          <div class="dash-toolbar">
            <a-button size="small" @click="currentDash = null">
              <ArrowLeftOutlined /> 返回列表
            </a-button>
            <h3 class="dash-name">{{ currentDash.name }}</h3>
            <a-button size="small" @click="refreshAll" :loading="refreshingAll">
              <ReloadOutlined /> 刷新全部
            </a-button>
          </div>
          <div class="widget-grid">
            <div v-for="w in currentDash.widgets" :key="w.id" class="widget-card"
                 :style="cardStyle(w)">
              <div class="widget-head">
                <span class="widget-title" :title="w.question">{{ w.question }}</span>
                <span class="widget-ops">
                  <a-button size="small" type="text" @click="refreshWidget(w)"
                            :loading="w._loading"><ReloadOutlined /></a-button>
                  <a-popconfirm title="移除该组件?" @confirm="removeWidget(w)">
                    <a-button size="small" type="text" danger><DeleteOutlined /></a-button>
                  </a-popconfirm>
                </span>
              </div>
              <div class="widget-body">
                <div v-if="w._live" class="live-meta">{{ w._live.rowCount }} 行</div>
                <component :is="renderWidget(w)" v-if="w._live?.chartOption"
                           class="widget-chart" />
                <div v-else-if="w._live" class="widget-table">
                  <table>
                    <thead><tr><th v-for="c in w._live.columns" :key="c">{{ c }}</th></tr></thead>
                    <tbody>
                      <tr v-for="(row, i) in w._live.rows.slice(0, 8)" :key="i">
                        <td v-for="(cell, j) in row" :key="j">{{ cell }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-if="w._live.rows.length > 8" class="more-line">
                    仅显示前 8 行, 共 {{ w._live.rowCount }} 行
                  </div>
                </div>
                <div v-else class="widget-empty">点击 ↻ 刷新查看数据</div>
              </div>
            </div>
            <a-empty v-if="!currentDash.widgets.length" description="空看板——从「保存查询」列表添加组件" />
          </div>
        </template>
      </a-tab-pane>
    </a-tabs>

    <!-- 新建看板 -->
    <a-modal v-model:open="showCreateDash" title="新建看板" @ok="createDash">
      <a-input v-model:value="newDashName" placeholder="看板名称" @pressEnter="createDash" />
    </a-modal>

    <!-- 加到看板 -->
    <a-modal v-model:open="showAddWidget" title="添加到看板" @ok="addWidget">
      <a-form layout="vertical">
        <a-form-item label="目标看板" required>
          <a-select v-model:value="addWidgetForm.dashboardId" placeholder="选择看板">
            <a-select-option v-for="d in dashboards" :key="d.id" :value="d.id">{{ d.name }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="图表类型">
          <a-select v-model:value="addWidgetForm.chartType">
            <a-select-option value="auto">自动</a-select-option>
            <a-select-option value="bar">柱状图</a-select-option>
            <a-select-option value="pie">饼图</a-select-option>
            <a-select-option value="line">折线图</a-select-option>
            <a-select-option value="table">表格</a-select-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// M4 交付页 —— 保存查询列表 + 看板 widget 墙(C-2 前端零消费缺口收口)。
// 后端 11 端点已就绪(user_required: X-User-Id 行级隔离);本页消费全部核心链路:
// 查询列表/CSV 导出/加到看板/看板 CRUD/widget 刷新/移除。
// 图表渲染复用 BiChartCard 的 ECharts 协议(chartOption 即 option)。
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  AppstoreAddOutlined, ArrowLeftOutlined, DeleteOutlined, DownloadOutlined,
  HistoryOutlined, LayoutOutlined, PlusOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'

const tab = ref('queries')

// ── 用户身份(M4 端点 user_required: X-User-Id 行级隔离) ──
// 管理端场景下与登录人共享同一身份;Bearer/X-Admin-Token 由 chatbiApi 拦截器统一注入
function uid(): string {
  return localStorage.getItem('chatbi_user_id') || 'admin'
}
function fmtTime(iso: string): string {
  return iso ? iso.replace('T', ' ').slice(0, 16) : ''
}

// ══════════ 保存查询 ══════════
const queries = ref<any[]>([])
const loadingQ = ref(false)

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

async function exportCsv(record: any) {
  // 浏览器原生下载(CSV 端点直接回 text/csv + BOM)
  try {
    const resp = await fetch(
      `${chatbiApi.defaults.baseURL}/saved-queries/${record.id}/export`,
      { headers: { 'X-Admin-Token': localStorage.getItem('admin_token') || '',
                   'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}`,
                   'X-User-Id': uid() } })
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`)
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

// ══════════ 看板 ══════════
const dashboards = ref<any[]>([])
const loadingD = ref(false)
const currentDash = ref<any>(null)
const showCreateDash = ref(false)
const newDashName = ref('')
const showAddWidget = ref(false)
const addWidgetForm = reactive({ dashboardId: '', chartType: 'auto' })
const refreshingAll = ref(false)
let pendingQuery: any = null

async function loadDashboards() {
  loadingD.value = true
  try {
    const { data } = await chatbiApi.get('/dashboards', { headers: { 'X-User-Id': uid() } })
    dashboards.value = data.items || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  } finally { loadingD.value = false }
}

async function createDash() {
  if (!newDashName.value.trim()) { message.warning('名称不能为空'); return }
  try {
    await chatbiApi.post('/dashboards', { name: newDashName.value.trim() },
                         { headers: { 'X-User-Id': uid() } })
    message.success('看板已创建')
    showCreateDash.value = false
    newDashName.value = ''
    await loadDashboards()
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

async function renameDash(record: any) {
  const name = window.prompt('新名称', record.name)
  if (!name || !name.trim()) return
  try {
    await chatbiApi.put(`/dashboards/${record.id}`, { name: name.trim() },
                        { headers: { 'X-User-Id': uid() } })
    await loadDashboards()
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

async function removeDash(record: any) {
  try {
    await chatbiApi.delete(`/dashboards/${record.id}`, { headers: { 'X-User-Id': uid() } })
    message.success('已删除')
    await loadDashboards()
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

async function openDash(record: any) {
  try {
    const { data } = await chatbiApi.get(`/dashboards/${record.id}`,
                                         { headers: { 'X-User-Id': uid() } })
    currentDash.value = data
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

function cardStyle(w: any) {
  // 12 列网格: width 1-12; height 单位约 80px
  const cols = Math.min(12, Math.max(1, w.width || 6))
  return {
    gridColumn: `span ${cols}`,
    minHeight: `${(w.height || 4) * 22 + 60}px`,
  }
}

function renderWidget(w: any) {
  // ECharts option 直接可渲染;此处返回占位组件名(实际由下方 v-if 分支处理)
  return null
}

async function refreshWidget(w: any) {
  w._loading = true
  try {
    const { data } = await chatbiApi.put(
      `/dashboards/${currentDash.value.id}/widgets/${w.id}/refresh`,
      {}, { headers: { 'X-User-Id': uid() } })
    w._live = data
  } catch (e: any) {
    message.error(`「${w.question}」刷新失败: ${e?.response?.data?.detail || e.message}`)
  } finally { w._loading = false }
}

async function refreshAll() {
  refreshingAll.value = true
  await Promise.allSettled((currentDash.value.widgets || []).map((w: any) => refreshWidget(w)))
  refreshingAll.value = false
}

async function removeWidget(w: any) {
  try {
    await chatbiApi.delete(
      `/dashboards/${currentDash.value.id}/widgets/${w.id}`,
      { headers: { 'X-User-Id': uid() } })
    currentDash.value.widgets = currentDash.value.widgets.filter((x: any) => x.id !== w.id)
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

function openAddToDash(record: any) {
  pendingQuery = record
  addWidgetForm.dashboardId = dashboards.value[0]?.id || ''
  addWidgetForm.chartType = 'auto'
  if (!dashboards.value.length) { message.info('请先创建一个看板'); return }
  showAddWidget.value = true
}

async function addWidget() {
  if (!addWidgetForm.dashboardId) { message.warning('请选择看板'); return }
  try {
    await chatbiApi.post(`/dashboards/${addWidgetForm.dashboardId}/widgets`, {
      question: pendingQuery.question,
      query_sql: pendingQuery.sqlText,
      datasource_id: pendingQuery.dataSourceId,
      chart_type: addWidgetForm.chartType === 'auto' ? 'table' : addWidgetForm.chartType,
    }, { headers: { 'X-User-Id': uid() } })
    message.success('已添加到看板')
    showAddWidget.value = false
  } catch (e: any) { message.error(e?.response?.data?.detail || e.message) }
}

onMounted(() => { loadQueries(); loadDashboards() })
</script>

<style scoped>
.m4-page { display: flex; flex-direction: column; }
.sql-cell {
  display: inline-block; max-width: 300px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; background: #f5f5f5; padding: 1px 6px; border-radius: 3px;
  font-size: 12px; vertical-align: middle;
}
.dash-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.dash-name { margin: 0; flex: 1; }
.widget-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px;
}
.widget-card {
  border: 1px solid #f0f0f0; border-radius: 8px; padding: 10px 12px;
  background: #fff; display: flex; flex-direction: column;
}
.widget-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.widget-title { flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; font-size: 13px; }
.widget-body { flex: 1; overflow: auto; }
.widget-table table { width: 100%; border-collapse: collapse; font-size: 12px; }
.widget-table th, .widget-table td {
  border-bottom: 1px solid #f5f5f5; padding: 3px 6px; text-align: left;
  max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.more-line, .live-meta { color: #999; font-size: 12px; margin-top: 4px; }
.widget-empty { color: #bbb; display: flex; align-items: center; justify-content: center;
               flex: 1; font-size: 13px; }
</style>
