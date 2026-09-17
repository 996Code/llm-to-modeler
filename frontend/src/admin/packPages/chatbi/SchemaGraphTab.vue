<template>
  <div class="sgt-page">
    <!-- 工具条:数据源选择 + 搜索 + 缩放 + 适应 -->
    <div class="sgt-toolbar">
      <a-select v-model:value="dsId" style="min-width: 220px" placeholder="选择数据源"
                :options="(datasources || []).map((d: any) => ({ value: d.id, label: d.name }))"
                @change="loadGraph" />
      <a-input-search v-model:value="search" placeholder="搜索表名, 回车定位" style="width: 220px"
                      allow-clear @search="focusNode" />
      <a-button-group>
        <a-button @click="zoomBy(1.2)" title="放大"><ZoomInOutlined /></a-button>
        <a-button @click="zoomBy(1 / 1.2)" title="缩小"><ZoomOutOutlined /></a-button>
        <a-button @click="fitView" title="适应画布"><FullscreenOutlined /></a-button>
      </a-button-group>
      <a-divider type="vertical" />
      <a-radio-group v-model:value="mode" size="small">
        <a-radio-button value="browse">浏览</a-radio-button>
        <a-radio-button value="edit">编辑关系</a-radio-button>
      </a-radio-group>
      <span v-if="graphData" class="sgt-meta">
        {{ graphData.nodes.length }} 表 · {{ graphData.edges.length }} 关系
      </span>
    </div>

    <!-- 画布 -->
    <div class="sgt-canvas-wrap">
      <div v-if="loading" class="sgt-center"><a-spin tip="图谱加载中..." /></div>
      <div v-else-if="!graphData || !graphData.nodes.length" class="sgt-center">
        <a-empty description="暂无图谱数据——请先在数据源页完成扫描" />
      </div>
      <!-- 渲染期间保持挂载但降透明度, 避免"旧图→白屏→新图"的闪一下;
           loading 结束后淡入(120ms), 切换数据源的视觉连续性 -->
      <div v-show="!loading && graphData?.nodes.length" ref="box" class="sgt-canvas"
           :class="{ 'sgt-fade': !rendered }" />

      <!-- 详情面板(节点/边) -->
      <div v-if="selectedNode" class="sgt-panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">{{ selectedNode.label }}</div>
            <div class="panel-sub">{{ selectedNode.id }}</div>
          </div>
          <CloseOutlined class="panel-close" @click="selectedNode = null" />
        </div>
        <div class="panel-stats">
          <div class="p-stat"><b>{{ selectedNode.columnCount }}</b><span>列</span></div>
          <div class="p-stat"><b>{{ selectedNode.degree }}</b><span>关联</span></div>
          <div class="p-stat"><b>{{ selectedNode.metricCount }}</b><span>指标</span></div>
          <div class="p-stat"><b>{{ (selectedNode.centrality ?? 0).toFixed(2) }}</b><span>中心度</span></div>
        </div>
        <!-- 影响分析 -->
        <div class="panel-section">
          <a-button size="small" type="link" :loading="impactLoading === selectedNode.id"
                    @click="loadImpact(selectedNode.id)">
            影响分析
          </a-button>
          <div v-if="impactResult" class="section-content">
            <a-tag v-for="t in impactResult" :key="t" color="orange">{{ t }}</a-tag>
            <div v-if="!impactResult.length" class="muted">无影响表</div>
          </div>
        </div>
        <div v-if="nodeEdges(selectedNode.id).length" class="panel-section">
          <div class="section-title">关联关系</div>
          <div v-for="(e, i) in nodeEdges(selectedNode.id)" :key="i" class="rel-line">
            <code>{{ e.source }} → {{ e.target }}</code>
            <div class="rel-on">{{ e.on }}</div>
            <div class="rel-meta">{{ e.joinType }} · {{ e.cardinality }} · 置信度 {{ e.confidence }}
              <a-button v-if="mode === 'edit'" size="small" type="link" danger
                        @click="deleteRelationship(e)">删除</a-button>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="selectedEdge" class="sgt-panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">{{ selectedEdge.source }} → {{ selectedEdge.target }}</div>
            <div class="panel-sub">关系详情</div>
          </div>
          <CloseOutlined class="panel-close" @click="selectedEdge = null" />
        </div>
        <div class="panel-section">
          <div class="kv"><span>ON 条件</span><code>{{ selectedEdge.on }}</code></div>
          <div class="kv"><span>JOIN 类型</span><code>{{ selectedEdge.joinType }}</code></div>
          <div class="kv"><span>基数</span><code>{{ selectedEdge.cardinality }}</code></div>
          <div class="kv"><span>置信度</span><code>{{ selectedEdge.confidence }}</code></div>
          <div class="kv"><span>来源</span><code>{{ relSourceLabel(selectedEdge.relSource) }}</code></div>
          <div v-if="mode === 'edit'" style="margin-top:8px;text-align:right">
            <a-button size="small" danger @click="deleteRelationship(selectedEdge)">删除关系</a-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑模式: 新增关系入口 -->
    <div v-if="mode === 'edit'" class="sgt-edit-bar">
      <a-button type="primary" size="small" @click="openAddRel">
        <PlusOutlined /> 新增关系
      </a-button>
      <span class="muted">选择源表/目标表, 填写 JOIN ON 条件——写回语义层新版本并重建索引</span>
    </div>

    <!-- 新增关系弹窗 -->
    <a-modal v-model:open="showAddRel" title="新增关系" ok-text="添加" cancel-text="取消"
             :confirm-loading="addRelLoading" @ok="addRelationship">
      <a-form layout="vertical">
        <div class="form-row">
          <a-form-item label="源表" required class="half">
            <a-select v-model:value="addRelForm.from" show-search
                      :options="tableOptions" placeholder="选择源表" />
          </a-form-item>
          <a-form-item label="目标表" required class="half">
            <a-select v-model:value="addRelForm.target" show-search
                      :options="tableOptions" placeholder="选择目标表" />
          </a-form-item>
        </div>
        <a-form-item label="JOIN 条件 (ON)" required>
          <!-- 结构化列选择(源 ChatBI 语义): 只能选真实列, 杜绝拼写错误/
               无效列进语义层;多行 = 多列 JOIN(AND 连接) -->
          <div v-for="(c, i) in onConditions" :key="i" class="on-row">
            <a-select v-model:value="c.left" :options="colOptions(fromColumns)"
                      placeholder="源表列" show-search style="flex: 1" />
            <span class="on-eq">=</span>
            <a-select v-model:value="c.right" :options="colOptions(targetColumns)"
                      placeholder="目标表列" show-search style="flex: 1" />
            <a-button v-if="onConditions.length > 1" size="small" type="text" danger
                      @click="onConditions.splice(i, 1)">−</a-button>
          </div>
          <a-button size="small" type="dashed" block style="margin-top: 4px"
                    @click="onConditions.push({ left: '', right: '' })">
            + 加一列条件 (AND)
          </a-button>
        </a-form-item>
        <div class="form-row">
          <a-form-item label="JOIN 类型" class="half">
            <a-select v-model:value="addRelForm.joinType">
              <a-select-option value="LEFT">LEFT</a-select-option>
              <a-select-option value="INNER">INNER</a-select-option>
              <a-select-option value="RIGHT">RIGHT</a-select-option>
              <a-select-option value="FULL">FULL</a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item label="基数" class="half">
            <a-select v-model:value="addRelForm.cardinality">
              <a-select-option value="N:1">N:1</a-select-option>
              <a-select-option value="1:N">1:N</a-select-option>
              <a-select-option value="1:1">1:1</a-select-option>
              <a-select-option value="N:N">N:N</a-select-option>
            </a-select>
          </a-form-item>
        </div>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 智能问数·图谱 Tab:数据源表关系图谱(G6 v5 动态导入,复用 KgGraphCard 的
// 成熟配置:力导布局/悬停高亮/tooltip)。数据来自 GET /datasources/{id}/graph
// (to_vis_data: 节点含社区/中心度, 边含 ON/置信度)。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  CloseOutlined, FullscreenOutlined, PlusOutlined, ZoomInOutlined, ZoomOutOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'

const props = defineProps<{
  dsId: string                                  // 父级指定的数据源(数据源页「图谱」入口)
  datasources: { id: string; name: string }[]   // 数据源下拉选项
}>()

const dsId = ref(props.dsId || '')
const search = ref('')
const loading = ref(false)
const graphData = ref<{ nodes: any[]; edges: any[] } | null>(null)
const selectedNode = ref<any>(null)
const selectedEdge = ref<any>(null)
const mode = ref<'browse' | 'edit'>('browse')
const semanticVersion = ref<number | null>(null)  // 图谱编辑的版本前置

// 影响分析
const impactLoading = ref('')
const impactResult = ref<string[] | null>(null)

// 新增关系
const showAddRel = ref(false)
const addRelLoading = ref(false)
const addRelForm = reactive({ from: '', target: '', joinType: 'LEFT', cardinality: 'N:1' })
// 结构化 ON 条件(列级选择, 后端按表/列真实性校验——替代自由文本)
const onConditions = ref<{ left: string; right: string }[]>([{ left: '', right: '' }])
const fromColumns = ref<string[]>([])
const targetColumns = ref<string[]>([])
const tableOptions = computed(() => (graphData.value?.nodes || []).map((n) => ({ value: n.id, label: n.label || n.id })))
const colOptions = (cols: string[]) => cols.map((c) => ({ value: c, label: c }))

// 选表变化 → 拉该表列清单(table-columns API), 列下拉只给真实列
async function loadColumns(table: string, side: 'from' | 'target') {
  if (!table || !dsId.value) { if (side === 'from') fromColumns.value = []; else targetColumns.value = []; return }
  try {
    const { data } = await chatbiApi.get(`/datasources/${dsId.value}/graph/table-columns`, { params: { table } })
    if (side === 'from') fromColumns.value = (data.columns || []).map((c: any) => c.name)
    else targetColumns.value = (data.columns || []).map((c: any) => c.name)
  } catch {
    if (side === 'from') fromColumns.value = []
    else targetColumns.value = []
  }
}

watch(() => addRelForm.from, (v) => loadColumns(v, 'from'))
watch(() => addRelForm.target, (v) => loadColumns(v, 'target'))

async function loadImpact(table: string) {
  impactLoading.value = table
  impactResult.value = null
  try {
    const { data } = await chatbiApi.get(`/datasources/${dsId.value}/graph/impact`, { params: { table } })
    impactResult.value = data.impact || []
  } catch {
    impactResult.value = []
  } finally {
    impactLoading.value = ''
  }
}

function openAddRel() {
  addRelForm.from = selectedNode.value?.id || ''
  addRelForm.target = ''
  addRelForm.joinType = 'LEFT'
  addRelForm.cardinality = 'N:1'
  onConditions.value = [{ left: '', right: '' }]
  fromColumns.value = []
  targetColumns.value = []
  showAddRel.value = true
  // openAddRel 可能先于 watch 触发拉列(表已选中场景)
  if (addRelForm.from) loadColumns(addRelForm.from, 'from')
}

async function addRelationship() {
  // ON 条件由结构化行构建(至少一行且两侧列都选齐)
  const pairs = onConditions.value.filter((c) => c.left && c.right)
  if (semanticVersion.value == null) {
      message.warning('图谱版本未加载——请先刷新页面')
      return
    }
    if (!addRelForm.from || !addRelForm.target || !pairs.length) {
    message.warning('请填写源表、目标表, 并至少配一对 JOIN 列')
    return
  }
  addRelLoading.value = true
  try {
    const on = pairs.map((c) => `${addRelForm.from}.${c.left} = ${addRelForm.target}.${c.right}`).join(' AND ')
    const { data } = await chatbiApi.post(`/datasources/${dsId.value}/graph/relationship`, {
      from_table: addRelForm.from,
      target_table: addRelForm.target,
      join_type: addRelForm.joinType,
      on,
      cardinality: addRelForm.cardinality,
      expected_version: semanticVersion.value,  // 十一审 7.3: 版本前置
    })
    if (data.index_rebuilt === false) {
      message.warning(`关系已添加(语义层 v${data.version}), 但索引重建失败——检索可能滞后, 手动重扫可修复`)
    } else {
      message.success(`关系已添加, 语义层新版本 v${data.version} 已生成`)
    }
    showAddRel.value = false
    await loadGraph()
  } catch (e: any) {
    message.warning(e?.response?.status === 409 ? '图谱已被其他管理员更新——请刷新后重试' : (e?.response?.data?.detail || '添加失败'))
  } finally {
    addRelLoading.value = false
  }
}

async function deleteRelationship(edge: any) {
  if (!edge) return
  // 取消直接返回——此前用"确认才 resolve"的 Promise, 取消时永不完成,
  // 处理函数被永久挂起(复核报告 P0)
  if (!window.confirm(`删除关系 ${edge.source} → ${edge.target}? (反向关系一并删除)`)) return
  try {
    const { data } = await chatbiApi.delete(`/datasources/${dsId.value}/graph/relationship`, {
      params: { from_table: edge.source, target_table: edge.target, on: edge.on || '',
                expected_version: semanticVersion.value },
    })
    semanticVersion.value = data.version
    const n = (data.removed_forward || 0) + (data.removed_reverse || 0)
    if (data.index_rebuilt === false) {
      message.warning(`已删除 ${n} 条关系(语义层 v${data.version}), 但索引重建失败——手动重扫可修复`)
    } else {
      message.success(`已删除 ${n} 条关系`)
    }
    selectedNode.value = null
    selectedEdge.value = null
    await loadGraph()
  } catch (e: any) {
    message.warning(e?.response?.status === 409 ? '图谱已被其他管理员更新——请刷新后重试' : (e?.response?.data?.detail || '删除失败'))
  }
}

const box = ref<HTMLElement | null>(null)
let graph: import('@antv/g6').Graph | null = null
// 渲染完成前画布降透明度(配合 .sgt-fade 淡入), 消除重绘白屏闪烁
const rendered = ref(false)

const _PALETTE = ['#5B8FF9', '#5AD8A6', '#F6BD16', '#E86452', '#6DC8EC',
                  '#945FB9', '#FF9845', '#1E9493', '#FF99C3', '#269A99']

watch(() => props.dsId, (v) => { if (v) dsId.value = v })
watch(dsId, (v) => { if (v) loadGraph() })
// 数据源下拉选项就绪但尚未选中时, 默认选第一个(含挂载即就绪的场景)
watch(() => props.datasources, (list) => {
  if (!dsId.value && list?.length) dsId.value = list[0].id
})

// 挂载时兜底: props 已有值但 watch 未触发的场景
// (dsId 非空 → 直接选中; datasources 非空且未选中 → 选第一个)
onMounted(() => {
  if (props.dsId) {
    dsId.value = props.dsId
  } else if (props.datasources?.length) {
    dsId.value = props.datasources[0].id
  }
})

async function loadGraph() {
  if (!dsId.value) return
  loading.value = true
  selectedNode.value = null
  selectedEdge.value = null
  try {
    const { data } = await chatbiApi.get(`/datasources/${dsId.value}/graph`)
    graphData.value = data
    semanticVersion.value = data.semanticVersion ?? null  // 十一审 7.3
    // 等容器真正可见(切 Tab 首次挂载时 v-if 已保证, 但 keep 场景/首帧
    // 布局未稳定时 offsetWidth 可能为 0)——双 rAF 确保布局完成再量尺寸
    await nextFrame()
    await render()
  } catch (e: any) {
    graphData.value = null
  } finally {
    loading.value = false
  }
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
}

function relSourceLabel(s: string): string {
  return { manual: '人工标注', manual_edit: '人工标注', db_comment: '数据库注释', fk: '外键推断', llm: 'LLM 推断', auto_inferred: '自动推断' }[s] || s || '-'
}

function nodeEdges(nodeId: string) {
  return (graphData.value?.edges || []).filter(
    (e) => e.source === nodeId || e.target === nodeId)
}

async function render() {
  destroy()
  rendered.value = false
  if (!box.value || !graphData.value?.nodes.length) return
  const G6 = await import('@antv/g6')
  if (!box.value || !graphData.value) return  // 卸载竞态

  const nodes = graphData.value.nodes
  const edges = graphData.value.edges

  // 画布尺寸取外层 wrap(固定视口), 不取 box——G6 渲染后会把 box 自身撑高
  // (121 节点实测 8440px), 二次 render 时 offsetHeight 已被污染
  const wrap = box.value.parentElement
  let W = (wrap && wrap.offsetWidth) || box.value.offsetWidth || 800
  let H = (wrap && wrap.offsetHeight) || 520
  // 容器尚未布局(Tab 刚激活/父级 display:none 刚解除)时尺寸为 0——
  // 等一帧再量, 仍为 0 则用缺省值兜底, 绝不创建 0×0 画布(首绘空白的根因)
  if (!W || !H) {
    await nextFrame()
    W = (wrap && wrap.offsetWidth) || 800
    H = (wrap && wrap.offsetHeight) || 520
  }

  graph = new G6.Graph({
    container: box.value,
    width: W,
    height: H,
    animation: false,
    data: {
      nodes: nodes.map((n) => ({
        id: n.id,
        data: {
          label: n.label || n.id, community: n.community ?? 0,
          deg: n.degree ?? 0, columnCount: n.columnCount ?? 0,
          metricCount: n.metricCount ?? 0, centrality: n.centrality ?? 0,
        },
      })),
      edges: edges.map((e, i) => ({
        id: `e-${i}`, source: e.source, target: e.target,
        data: { on: e.on || '', confidence: e.confidence ?? 0,
                joinType: e.joinType || '', cardinality: e.cardinality || '',
                relSource: e.relSource || '' },
      })),
    },
    node: {
      style: {
        size: (d: any) => 16 + Math.min(24, (d.data?.deg || 0) * 5),
        fill: (d: any) => _PALETTE[(d.data?.community || 0) % _PALETTE.length],
        stroke: '#fff', lineWidth: 2, cursor: 'pointer',
        labelText: (d: any) => d.data?.label || d.id,
        labelFontSize: 10, labelFill: '#374151', labelPlacement: 'right',
      },
      state: {
        highlight: { stroke: '#D580FF', lineWidth: 3 },
        dim: { fillOpacity: 0.25, strokeOpacity: 0.25, labelOpacity: 0.25 },
      },
    },
    edge: {
      type: 'quadratic',
      style: {
        stroke: '#94a3b8', lineWidth: 1.1, endArrow: true, endArrowSize: 5,
        cursor: 'pointer',
      },
      state: {
        highlight: { stroke: '#D580FF', lineWidth: 2 },
        dim: { strokeOpacity: 0.12 },
      },
    },
    layout: {
      // 原版 g6-config.ts D3_FORCE_LAYOUT 同款参数(121 表大图验证过的配置)
      type: 'd3-force', preLayout: true, preventOverlap: true,
      linkDistance: 200, nodeStrength: -400, edgeStrength: 0.1,
      collideStrength: 0.8, alphaDecay: 0.05, alphaMin: 0.001,
    },
    plugins: [{
      type: 'tooltip',
      getContent: (_e: unknown, items: { id: string; source?: string; data: Record<string, unknown> }[]) => {
        const it = items?.[0]
        if (!it) return '<div></div>'
        const d = it.data || {}
        const body = it.source !== undefined
          ? `<div>${_esc(d.on || '')}</div><div>${_esc(String(d.joinType || ''))} · 置信度 ${_esc(String(d.confidence ?? ''))}</div>`
          : `<div><b>${_esc(String(d.label || ''))}</b></div><div>${_esc(String(d.columnCount ?? 0))} 列 · ${_esc(String(d.deg ?? 0))} 关联 · 中心度 ${_esc(String(d.centrality ?? 0))}</div>`
        return `<div style="background:rgba(0,0,0,0.78);color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;line-height:1.6;max-width:280px;word-break:break-all">${body}</div>`
      },
    }],
    behaviors: [
      { type: 'drag-canvas' }, { type: 'zoom-canvas' }, { type: 'drag-element' },
      { type: 'hover-activate', degree: 1, state: 'highlight', inactiveState: 'dim',
        enable: (e: { targetType: string }) => e.targetType === 'node' },
    ],
  })

  graph.on('node:click', (e: any) => {
    const id = e.target?.id
    selectedEdge.value = null
    selectedNode.value = graphData.value?.nodes.find((n) => n.id === id) || null
  })
  graph.on('edge:click', (e: any) => {
    const src = e.target?.source, tgt = e.target?.target
    selectedNode.value = null
    selectedEdge.value = graphData.value?.edges.find(
      (x) => (x.source === src && x.target === tgt)) || null
  })

  try {
    await graph.render()
    await graph.fitView()
    if (graph && graph.getZoom() < 0.8) {
      graph.zoomTo(0.8)
      await graph.fitCenter()
    }
    rendered.value = true
  } catch { /* 渲染中断(切页) */ }

  // G6 v5 + d3-force: 布局收敛/尺寸稳定后校准一次画布尺寸(容器隐藏时
  // 首绘可能是空画布/错位)。仅在尺寸确实变化时 resize, 避免无谓重绘闪烁。
  setTimeout(() => {
    if (!graph || !box.value) return
    try {
      const wrap2 = box.value.parentElement
      const w2 = (wrap2 && wrap2.offsetWidth) || W
      const h2 = (wrap2 && wrap2.offsetHeight) || H
      if (w2 !== W || h2 !== H) {
        graph.resize(w2, h2)
        graph.render()
      }
    } catch { /* 已销毁 */ }
  }, 400)
}

function _esc(s: unknown): string {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string))
}

function focusNode() {
  const q = search.value.trim().toLowerCase()
  if (!q || !graph || !graphData.value) return
  const hit = graphData.value.nodes.find(
    (n) => n.id.toLowerCase().includes(q) || (n.label || '').toLowerCase().includes(q))
  if (!hit) return
  selectedEdge.value = null
  selectedNode.value = hit
  graph.focusElement(hit.id, true)
}

function zoomBy(factor: number) {
  if (!graph) return
  graph.zoomTo(graph.getZoom() * factor)
}

function fitView() {
  graph?.fitView()
}

function destroy() {
  if (graph) { try { graph.destroy() } catch { /* 已销毁 */ } graph = null }
}

onBeforeUnmount(destroy)
</script>

<style scoped>
.on-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.on-eq { color: #86909c; flex-shrink: 0; }
.sgt-page { display: flex; flex-direction: column; gap: 10px; height: calc(100vh - 220px); min-height: 480px; }
.sgt-toolbar { display: flex; align-items: center; gap: 10px; }
.sgt-meta { color: #86909c; font-size: 12px; margin-left: auto; }
.sgt-canvas-wrap {
  position: relative; flex: 1; border: 1px solid #eef1f7; border-radius: 10px;
  background: #fff; overflow: hidden;
}
.sgt-canvas { position: absolute; inset: 0; overflow: hidden; }
/* 重绘期间降透明度, 渲染完成后淡入——消除切换数据源时"旧图闪没→新图蹦出" */
.sgt-fade { opacity: 0; transition: opacity 0.12s ease-out; }
.sgt-canvas:not(.sgt-fade) { opacity: 1; }
/* G6 v5 渲染时给容器内联 position:relative, 并给每层 canvas 内联
   grid-area:1/1/2/2(预期父级 display:grid 层叠)。内联样式覆盖了上面的
   scoped absolute → 容器被内容撑高(实测 1856px), 画了图的那层 canvas
   被顶出 wrap 可视区——页面看起来"空白"(图谱 tab 空白的真正根因)。
   !important 压回内联; display:grid 让多层 canvas(背景/主内容/前景)
   按内联 grid-area 叠在同一格 */
.sgt-canvas { position: absolute !important; inset: 0 !important; display: grid !important; }
.sgt-canvas canvas { grid-area: 1 / 1 / 2 / 2; }
.sgt-center {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
}

/* 详情面板 */
.sgt-panel {
  position: absolute; top: 12px; right: 12px; width: 300px;
  background: #fff; border: 1px solid #e5e6eb; border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.08); padding: 12px 14px;
  max-height: calc(100% - 24px); overflow-y: auto;
}
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.panel-title { font-weight: 700; font-size: 14px; color: #1f2329; }
.panel-sub { font-size: 12px; color: #86909c; margin-top: 2px; }
.panel-close { color: #86909c; cursor: pointer; font-size: 13px; padding: 2px; }
.panel-close:hover { color: #1f2329; }
.panel-stats { display: flex; gap: 8px; margin: 10px 0; }
.p-stat {
  flex: 1; background: #f7f8fa; border-radius: 8px; padding: 8px 0; text-align: center;
}
.p-stat b { display: block; font-size: 15px; color: #1f2329; }
.p-stat span { font-size: 11px; color: #86909c; }
.panel-section .section-title { font-size: 12px; font-weight: 600; color: #4e5969; margin-bottom: 6px; }
.rel-line { padding: 6px 0; border-top: 1px solid #f5f5f6; }
.rel-line code { font-size: 12px; color: #3370ff; }
.rel-on { font-size: 11px; color: #4e5969; margin-top: 2px; word-break: break-all; }
.rel-meta { font-size: 11px; color: #86909c; margin-top: 1px; }
.kv { display: flex; justify-content: space-between; gap: 10px; padding: 5px 0; border-top: 1px solid #f5f5f6; font-size: 12px; }
.kv span { color: #86909c; flex-shrink: 0; }
.kv code { word-break: break-all; text-align: right; }
.sgt-edit-bar {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px;
  background: #f7f8fa; border-radius: 8px; margin-top: 4px;
}
.sgt-edit-bar .muted { color: #86909c; font-size: 12px; }
.section-content { margin-top: 6px; }
.section-content .ant-tag { margin: 2px; }
.form-row { display: flex; gap: 12px; }
.form-row .half { flex: 1; }
</style>
