<template>
  <div class="dash-page">
    <!-- 看板 Tab 列表(横向页签,对标原版 DashboardView) -->
    <div v-if="dashboards.length" class="dash-tabs">
      <div
        v-for="d in dashboards" :key="d.id"
        :class="['dash-tab', { active: currentDashId === d.id }]"
        @click="openDashboard(d.id)"
      >
        <span class="tab-name">{{ d.name }}</span>
        <a-dropdown :trigger="['click']">
          <MoreOutlined class="tab-more" @click.stop />
          <template #overlay>
            <a-menu @click="({ key }: any) => dashMenu(key, d)">
              <a-menu-item key="rename"><EditOutlined /> 重命名</a-menu-item>
              <a-menu-item key="delete" danger><DeleteOutlined /> 删除</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </div>

    <!-- 工具条 -->
    <div class="dash-toolbar">
      <a-button type="primary" size="small" @click="openCreateDash">
        <PlusOutlined /> 新建看板
      </a-button>
      <a-button size="small" :type="editMode ? 'primary' : 'default'" @click="toggleEditMode">
        <template #icon>
          <CheckOutlined v-if="editMode" /><EditOutlined v-else />
        </template>
        {{ editMode ? '完成布局' : '编辑布局' }}
      </a-button>
      <!-- 刷新全部:与 widget 内刷新同款 svg 图标(风格统一), 悬停提示 -->
      <a-tooltip title="刷新全部">
        <button class="tb-icon-btn" :disabled="!currentDashId || refreshingAll"
                @click="refreshAll()">
          <ReloadOutlined :spin="refreshingAll" />
        </button>
      </a-tooltip>
    </div>

    <!-- 看板墙: gridstack 拖拽布局;编辑模式加高亮背景标识可编辑区域 -->
    <div v-if="currentDashId" :class="['grid-container', { 'grid-editing': editMode }]">
      <div ref="gridEl" class="grid-stack"></div>
    </div>
    <a-empty v-if="currentDashId && !widgets.length"
             description="空看板——从「保存查询」列表或对话中添加组件" style="padding: 60px 0" />
    <a-empty v-if="!dashboards.length" description="暂无看板——新建一个, 再添加组件" style="padding: 60px 0">
      <a-button type="primary" @click="openCreateDash">新建看板</a-button>
    </a-empty>

    <!-- 新建/重命名看板 -->
    <a-modal v-model:open="showDashModal" :title="dashModalMode === 'create' ? '新建看板' : '重命名看板'"
             ok-text="确定" cancel-text="取消" :confirm-loading="dashModalLoading" @ok="submitDashModal">
      <a-input v-model:value="dashNameInput" placeholder="看板名称" :maxlength="50" show-count
               @pressEnter="submitDashModal" />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 看板子页:gridstack 拖拽布局(对标原版 DashboardView)+ ECharts 渲染。
// 布局持久化:编辑模式拖动 → PUT /dashboards/{id}/widgets/layout 批量落库。
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckOutlined, DeleteOutlined, EditOutlined, MoreOutlined, PlusOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { GridStack } from 'gridstack'
import 'gridstack/dist/gridstack.min.css'
import * as echarts from 'echarts'
import { chatbiApi } from '../../api'

function uid(): string {
  return localStorage.getItem('chatbi_user_id') || 'admin'
}
const H = { 'X-User-Id': uid() }

const dashboards = ref<any[]>([])
const currentDashId = ref('')
const widgets = ref<any[]>([])
const refreshingAll = ref(false)

// gridstack 实例 + 编辑模式
const gridEl = ref<HTMLElement>()
let grid: GridStack | null = null
const editMode = ref(false)
const chartInstances: Record<string, echarts.ECharts> = {}
// 每个 widget 的容器观察器(容器尺寸变化 → echarts resize)
const resizeObservers: Record<string, ResizeObserver> = {}

// 新建/重命名弹窗
const showDashModal = ref(false)
const dashModalMode = ref<'create' | 'rename'>('create')
const dashNameInput = ref('')
const dashModalLoading = ref(false)
let renameTarget: any = null

async function loadDashboards() {
  try {
    const { data } = await chatbiApi.get('/dashboards', { headers: H })
    dashboards.value = data.items || []
    if (!currentDashId.value && dashboards.value.length) {
      await openDashboard(dashboards.value[0].id)
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  }
}

async function openDashboard(did: string) {
  currentDashId.value = did
  try {
    const { data } = await chatbiApi.get(`/dashboards/${did}`, { headers: H })
    widgets.value = data.widgets || []
    await nextTick()
    initGrid()
    // 打开看板即自动刷新全部 widget(实时数据, 不让用户逐个点刷新)
    await refreshAll(true)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '打开失败')
  }
}
defineExpose({ openDashboard })

// ── gridstack 初始化(每次打开/切看板重建) ──
// 用真实 DOM 元素 + makeWidget(原版同款):addWidget 的 content 字符串
// 会被 gridstack 当纯文本插入(不解析 HTML), 按钮与结构全部失效。
async function initGrid() {
  destroyGrid()
  if (!gridEl.value) return
  const g = GridStack.init({
    column: 12, cellHeight: 70, margin: 8,   // 原版同款参数
    disableDrag: !editMode.value,
    disableResize: !editMode.value,
    staticGrid: !editMode.value,
    animate: true,
  }, gridEl.value)
  if (!g) return
  grid = g

  for (const w of widgets.value) {
    g.makeWidget(createWidgetEl(w))
  }
  // gridstack 布局是异步的(makeWidget 后下一帧才有最终宽高)——
  // 等两帧再渲染图表, echarts init 时容器已展开, 不会缩成小块
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
  for (const w of widgets.value) renderWidgetContent(w)

  // 拖拽/缩放结束 → 增量保存受影响项(原版同款: change 事件带 changedItems)
  g.on('change', ((_event: unknown, ...args: unknown[]) => {
    if (!editMode.value) return
    const changed = (Array.isArray(args[0]) ? args[0] : []) as Array<{ el?: HTMLElement }>
    const layout = changed.map((it) => {
      const el = it.el
      if (!el) return null
      const node = g.engine.nodes.find((n) => n.el === el)
      return {
        id: el.dataset.wid || '',
        position_x: node?.x ?? 0, position_y: node?.y ?? 0,
        width: node?.w ?? 6, height: node?.h ?? 4,
      }
    }).filter((i): i is { id: string; position_x: number; position_y: number; width: number; height: number } => !!i?.id)
    if (layout.length) saveLayout(layout)
  }) as any)
}

// 从 gridstack 内部引擎读布局(makeWidget 后 gs-x 属性会被引擎接管,
// dataset 读不到引擎维护的最新值——原版同款坑)
function readLayout(): Array<{ id: string; position_x: number; position_y: number; width: number; height: number }> {
  if (!grid) return []
  return grid.getGridItems().map((el) => {
    const node = grid!.engine.nodes.find((n) => n.el === el)
    const wid = (el as HTMLElement).dataset.wid || ''
    return {
      id: wid,
      position_x: node?.x ?? parseInt((el as HTMLElement).getAttribute('gs-x') || '0', 10),
      position_y: node?.y ?? parseInt((el as HTMLElement).getAttribute('gs-y') || '0', 10),
      width: node?.w ?? parseInt((el as HTMLElement).getAttribute('gs-w') || '6', 10),
      height: node?.h ?? parseInt((el as HTMLElement).getAttribute('gs-h') || '4', 10),
    }
  }).filter((i) => i.id)
}

function destroyGrid() {
  Object.values(chartInstances).forEach((c) => c.dispose())
  for (const k of Object.keys(chartInstances)) delete chartInstances[k]
  Object.values(resizeObservers).forEach((ro) => ro.disconnect())
  for (const k of Object.keys(resizeObservers)) delete resizeObservers[k]
  if (grid) { grid.destroy(false); grid = null }
  if (gridEl.value) gridEl.value.innerHTML = ''
}

function createWidgetEl(w: any): HTMLElement {
  const el = document.createElement('div')
  el.className = 'grid-stack-item'
  el.dataset.wid = w.id
  el.setAttribute('gs-x', String(w.positionX ?? 0))
  el.setAttribute('gs-y', String(w.positionY ?? 0))
  el.setAttribute('gs-w', String(w.width || 6))
  el.setAttribute('gs-h', String(w.height || 4))
  el.innerHTML = widgetHtml(w)
  return el
}

function widgetHtml(w: any): string {
  return `
    <div class="widget-card" data-wid="${w.id}">
      <div class="widget-head">
        <span class="widget-title" title="${escapeHtml(w.question)}">${escapeHtml(w.question)}</span>
        <span class="widget-ops">
          <button class="w-btn" data-act="refresh" title="刷新">
            <svg viewBox="0 0 1024 1024" width="13" height="13" fill="currentColor"><path d="M909.1 209.3l-56.4 44.1C775.8 155.1 656.2 92 521.9 92 290 92 102.3 279.5 102 511.5 101.7 743.7 289.8 932 521.9 932c181.3 0 335.8-117 391.6-278.7-1.2-.1-84.4-14.3-85.2-14.6-9 25.4-23.8 49.4-42.6 69.3-24.4 26-55.3 46.2-89.4 58.4-34.1 12.2-70.9 15.9-106.6 11-35.8-4.9-69.9-18.7-99.3-40-29.3-21.3-53.1-48.9-69.4-80.4-16.3-31.5-24.6-66.4-24.1-101.4.5-35 9.8-69.6 26.9-100.6 17.1-31 41.6-57.4 71.4-77.1 29.7-19.7 63.8-32.1 99-36.2 35.2-4.1 70.8-.2 104.1 11.3 33.3 11.6 63.4 30.4 88 54.9L672.7 499l241.4-8.7-5.1-281z"/></svg>
          </button>
          <button class="w-btn w-btn-danger" data-act="remove" title="移除">
            <svg viewBox="0 0 1024 1024" width="13" height="13" fill="currentColor"><path d="M563.8 512l262.5-312.9c12.1-14.4 10.2-35.9-4.2-48-14.4-12.1-35.9-10.2-48 4.2L512 464.4 249.9 155.3c-12.1-14.4-33.6-16.3-48-4.2-14.4 12.1-16.3 33.6-4.2 48L460.2 512 197.7 824.9c-12.1 14.4-10.2 35.9 4.2 48 6.4 5.4 14.2 8 21.9 8 9.7 0 19.4-4.2 26.1-12.2L512 559.6l262.1 313.1c6.7 8 16.4 12.2 26.1 12.2 7.7 0 15.5-2.6 21.9-8 14.4-12.1 16.3-33.6 4.2-48L563.8 512z"/></svg>
          </button>
        </span>
      </div>
      <div class="widget-body" id="wb-${w.id}">
        <div class="widget-empty">加载中...</div>
      </div>
      <div class="widget-footer" id="wf-${w.id}"></div>
    </div>`
}

function escapeHtml(s: string): string {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string))
}

// 事件委托:刷新/移除按钮(gridstack 生成的 DOM 不走 Vue 模板)
function onGridClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement).closest('[data-act]')
  if (!btn) return
  const card = (e.target as HTMLElement).closest('[data-wid]') as HTMLElement | null
  if (!card) return
  const wid = card.dataset.wid!
  const act = (btn as HTMLElement).dataset.act
  const w = widgets.value.find((x) => x.id === wid)
  if (!w) return
  if (act === 'refresh') refreshWidget(w)
  else if (act === 'remove') removeWidget(wid)
}

async function refreshWidget(w: any) {
  const body = document.getElementById(`wb-${w.id}`)
  if (body) body.innerHTML = '<div class="widget-empty">加载中...</div>'
  try {
    const { data } = await chatbiApi.put(
      `/dashboards/${currentDashId.value}/widgets/${w.id}/refresh`, {}, { headers: H })
    renderLive(w.id, data)
  } catch (e: any) {
    renderLive(w.id, {}, e?.response?.data?.detail || e.message || '刷新失败')
  }
}

function renderLive(wid: string, data: any, error?: string) {
  const body = document.getElementById(`wb-${wid}`)
  if (!body) return
  const footer = document.getElementById(`wf-${wid}`)
  chartInstances[wid]?.dispose()
  delete chartInstances[wid]

  // 出错(原版同款: 错误占位)
  if (error) {
    body.innerHTML = `<div class="widget-empty widget-error">${escapeHtml(error)}</div>`
    if (footer) footer.textContent = ''
    return
  }

  const chartOption = data.chartOption || data.chart_option
  const rows: any[][] = data.rows || []
  const hasData = rows.length > 0

  if (chartOption && hasData) {
    // 图表(原版同款: 容器 min-height 保底, nextTick 后 init)
    body.innerHTML = `<div class="widget-chart" style="width:100%;height:100%;min-height:200px"></div>`
    nextTick(() => {
      const el = body.querySelector('.widget-chart') as HTMLElement | null
      if (el) {
        const inst = echarts.init(el)
        inst.setOption(chartOption)
        chartInstances[wid] = inst
        // ResizeObserver 持续跟随容器:gridstack 布局/窗口缩放/编辑模式重建
        // 都会改变容器尺寸, echarts 不会自己跟着变——观察到变化即 resize。
        // (此前只在 init 后双 rAF 补一次, 覆盖不了布局晚于渲染的场景,
        //  图表一直缩在 280px 的根因)
        const ro = new ResizeObserver(() => {
          if (chartInstances[wid] && !chartInstances[wid].isDisposed()) {
            chartInstances[wid].resize()
          }
        })
        ro.observe(el)
        resizeObservers[wid] = ro
      }
    })
  } else if (data.columns?.length && hasData) {
    // 无图表但有数据 → 表格(原版 20 行 + more 提示)
    const cols: string[] = data.columns
    body.innerHTML = `
      <div class="widget-table-wrap">
        <table class="widget-table">
          <thead><tr>${cols.map((c) => `<th>${escapeHtml(String(c))}</th>`).join('')}</tr></thead>
          <tbody>${rows.slice(0, 20).map((r) =>
            `<tr>${r.map((cell) => `<td>${escapeHtml(String(cell ?? ''))}</td>`).join('')}</tr>`).join('')}</tbody>
        </table>
        ${(data.rowCount ?? rows.length) > 20 ? `<div class="more-line">共 ${data.rowCount ?? rows.length} 行, 仅展示前 20 行</div>` : ''}
      </div>`
  } else {
    body.innerHTML = `<div class="widget-empty">暂无数据</div>`
  }

  // 页脚(原版同款): 行数 · 刷新时间
  if (footer) {
    const parts: string[] = []
    if (data.rowCount != null) parts.push(`${data.rowCount} 行`)
    parts.push(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    footer.textContent = parts.join(' · ')
  }
}

function renderWidgetContent(w: any) {
  // 已有缓存数据(chartOption)直接渲染;没有则等自动刷新拉实时数据
  if (w.chartOption) renderLive(w.id, { chartOption: w.chartOption, rowCount: w.rowCount })
}

async function refreshAll(silent = false) {
  if (!widgets.value.length) return
  if (!silent) refreshingAll.value = true
  try {
    await Promise.all(widgets.value.map((w) => refreshWidget(w)))
    if (!silent) message.success('已刷新全部')
  } finally {
    refreshingAll.value = false
  }
}

async function removeWidget(wid: string) {
  try {
    await chatbiApi.delete(`/dashboards/${currentDashId.value}/widgets/${wid}`, { headers: H })
    widgets.value = widgets.value.filter((x) => x.id !== wid)
    const item = grid?.getGridItems().find((el) => (el as HTMLElement).dataset?.wid === wid)
    if (item && grid) grid.removeWidget(item)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '移除失败')
  }
}

async function toggleEditMode() {
  editMode.value = !editMode.value
  if (grid) {
    // 原版同款: 重建 grid 让 disableDrag/disableResize 配置生效
    // (setStatic 只冻结交互, 重建才能让拖拽/缩放手柄正确挂载)
    await nextTick()
    initGrid()
    // 编辑模式切换会重建 DOM(图表实例被销毁)——立即恢复各 widget 内容,
    // 编辑时看到的是真实图表而非"暂无数据"占位
    await refreshAll(true)
  }
  message.info(editMode.value ? '已进入编辑模式, 可拖拽/缩放组件' : '已退出编辑模式')
  if (!editMode.value) saveLayout(readLayout())
}

async function saveLayout(layout: Array<{ id: string; position_x: number; position_y: number; width: number; height: number }>) {
  if (!grid || !currentDashId.value || !layout.length) return
  try {
    await chatbiApi.put(`/dashboards/${currentDashId.value}/widgets/layout`,
                        { layout }, { headers: H })
  } catch (e: any) {
    message.error(`布局保存失败: ${e?.response?.data?.detail || e.message}`)
  }
}

// ── 看板 CRUD ──
function openCreateDash() {
  dashModalMode.value = 'create'
  dashNameInput.value = ''
  showDashModal.value = true
}

function dashMenu(key: string, d: any) {
  if (key === 'rename') {
    renameTarget = d
    dashModalMode.value = 'rename'
    dashNameInput.value = d.name
    showDashModal.value = true
  } else if (key === 'delete') {
    removeDash(d)
  }
}

async function submitDashModal() {
  const name = dashNameInput.value.trim()
  if (!name) { message.warning('请输入看板名称'); return }
  dashModalLoading.value = true
  try {
    if (dashModalMode.value === 'create') {
      const { data } = await chatbiApi.post('/dashboards', { name }, { headers: H })
      message.success('看板已创建')
      showDashModal.value = false
      await loadDashboards()
      if (data?.id) await openDashboard(data.id)
    } else if (renameTarget) {
      await chatbiApi.put(`/dashboards/${renameTarget.id}`, { name }, { headers: H })
      message.success('已重命名')
      showDashModal.value = false
      await loadDashboards()
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '操作失败')
  } finally {
    dashModalLoading.value = false
  }
}

async function removeDash(d: any) {
  try {
    await chatbiApi.delete(`/dashboards/${d.id}`, { headers: H })
    message.success(`看板「${d.name}」已删除`)
    if (currentDashId.value === d.id) {
      currentDashId.value = ''
      destroyGrid()
    }
    await loadDashboards()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '删除失败')
  }
}

onMounted(() => {
  loadDashboards()
  document.addEventListener('click', onGridClick)
  // 窗口尺寸变化 → 全部图表 resize(容器变了图表不会自己跟着变)
  window.addEventListener('resize', onWindowResize)
})

function onWindowResize() {
  Object.values(chartInstances).forEach((c) => {
    if (!c.isDisposed()) c.resize()
  })
}

onBeforeUnmount(() => {
  document.removeEventListener('click', onGridClick)
  window.removeEventListener('resize', onWindowResize)
  destroyGrid()
})
</script>

<style scoped>
.dash-page { display: flex; flex-direction: column; gap: 12px; }

/* 看板页签 */
.dash-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.dash-tab {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--border-color, #e5e6eb); background: #fff;
  font-size: 13px; color: var(--text-regular, #4e5969);
  transition: all 0.15s;
}
.dash-tab:hover { border-color: #a5b4fc; }
.dash-tab.active {
  background: var(--color-primary-light, #eaf0ff); color: var(--color-primary, #3370ff);
  border-color: var(--color-primary, #3370ff); font-weight: 600;
}
.tab-more { font-size: 12px; opacity: 0.45; padding: 2px; }
.tab-more:hover { opacity: 1; color: var(--color-primary, #3370ff); }

.dash-toolbar { display: flex; align-items: center; gap: 8px; }
.grid-container { border-radius: 10px; }

/* 工具条图标按钮:与 widget 内 .w-btn 同款规格(22px/透明底/hover 浅灰)——
   总刷新与单图表刷新视觉统一 */
.tb-icon-btn {
  width: 24px; height: 24px; margin-left: 4px;
  border: none; background: transparent; border-radius: 4px;
  cursor: pointer; color: #86909c; font-size: 13px;
  display: inline-flex; align-items: center; justify-content: center;
}
.tb-icon-btn:hover:not(:disabled) { background: #f2f3f5; color: #3370ff; }
.tb-icon-btn:disabled { opacity: 0.45; cursor: not-allowed; }

/* 编辑模式:hover 的 widget 淡色背景高亮(去掉整区底色, 只留悬停反馈,
   拖拽定位更方便)。gridstack 的 item 是运行时生成的 DOM(无 scoped
   属性), 必须 :deep */
.grid-editing { border-radius: 10px; padding: 6px; }
.grid-editing :deep(.grid-stack-item) { border-radius: 8px; transition: background 0.12s; }
.grid-editing :deep(.grid-stack-item:hover) {
  background: #eef3ff;     /* 淡蓝——只标识 hover 的这一个图表 */
  outline: 1.5px dashed #a5b4fc; outline-offset: -2px;
}

/* widget 卡片(gridstack 项内容)。注意:widget DOM 是运行时 createElement
   生成的(无 scoped 属性), 全部样式必须 :deep 才能命中——此前 .w-btn
   等规则 scoped 化后全部失效, 按钮退化成浏览器默认样式(灰色凸边按钮),
   这就是"刷新/删除还是朴素按钮"的根因 */
.grid-container :deep(.grid-stack-item-content) {
  border: 1px solid #f0f0f0; border-radius: 8px; background: #fff;
  overflow: hidden; display: flex; flex-direction: column;
}
.grid-container :deep(.widget-card) { display: flex; flex-direction: column; height: 100%; padding: 8px 10px; }
.grid-container :deep(.widget-head) { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.grid-container :deep(.widget-title) {
  flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 13px;
}
.grid-container :deep(.widget-ops) { display: flex; gap: 2px; }
.grid-container :deep(.w-btn) {
  width: 22px; height: 22px; padding: 0; border: none; background: transparent; border-radius: 4px;
  cursor: pointer; color: #86909c; font-size: 13px;
  display: inline-flex; align-items: center; justify-content: center;
}
.grid-container :deep(.w-btn:hover) { background: #f2f3f5; color: #3370ff; }
.grid-container :deep(.w-btn-danger:hover) { background: #fee; color: #f54a45; }
.grid-container :deep(.widget-body) { flex: 1; overflow: auto; min-height: 0; }
.grid-container :deep(.widget-empty) {
  color: #bbb; display: flex; align-items: center; justify-content: center;
  height: 100%; font-size: 13px;
}
.grid-container :deep(.widget-error) { color: #f54a45; }
.grid-container :deep(.widget-table) { width: 100%; border-collapse: collapse; font-size: 12px; }
.grid-container :deep(.widget-table th), .grid-container :deep(.widget-table td) {
  border-bottom: 1px solid #f5f5f5; padding: 3px 6px; text-align: left;
  max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.grid-container :deep(.more-line), .grid-container :deep(.live-meta) { color: #999; font-size: 12px; margin-top: 4px; }
.grid-container :deep(.widget-footer) {
  font-size: 11px; color: #bbb; padding: 4px 2px 0; text-align: right;
  border-top: 1px solid #f7f8fa; flex-shrink: 0;
}
.grid-container :deep(.widget-table-wrap) { height: 100%; overflow: auto; }
</style>
