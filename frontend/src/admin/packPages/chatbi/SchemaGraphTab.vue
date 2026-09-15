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
      <div v-show="!loading && graphData?.nodes.length" ref="box" class="sgt-canvas" />

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
        <div v-if="nodeEdges(selectedNode.id).length" class="panel-section">
          <div class="section-title">关联关系</div>
          <div v-for="(e, i) in nodeEdges(selectedNode.id)" :key="i" class="rel-line">
            <code>{{ e.source }} → {{ e.target }}</code>
            <div class="rel-on">{{ e.on }}</div>
            <div class="rel-meta">{{ e.joinType }} · {{ e.cardinality }} · 置信度 {{ e.confidence }}</div>
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
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 智能问数·图谱 Tab:数据源表关系图谱(G6 v5 动态导入,复用 KgGraphCard 的
// 成熟配置:力导布局/悬停高亮/tooltip)。数据来自 GET /datasources/{id}/graph
// (to_vis_data: 节点含社区/中心度, 边含 ON/置信度)。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CloseOutlined, FullscreenOutlined, ZoomInOutlined, ZoomOutOutlined,
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

const box = ref<HTMLElement | null>(null)
let graph: import('@antv/g6').Graph | null = null

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
    await render()
  } catch (e: any) {
    graphData.value = null
  } finally {
    loading.value = false
  }
}

function relSourceLabel(s: string): string {
  return { manual: '人工标注', fk: '外键推断', llm: 'LLM 推断', auto_inferred: '自动推断' }[s] || s || '-'
}

function nodeEdges(nodeId: string) {
  return (graphData.value?.edges || []).filter(
    (e) => e.source === nodeId || e.target === nodeId)
}

async function render() {
  destroy()
  if (!box.value || !graphData.value?.nodes.length) return
  const G6 = await import('@antv/g6')
  if (!box.value || !graphData.value) return  // 卸载竞态

  const nodes = graphData.value.nodes
  const edges = graphData.value.edges

  // 画布尺寸取外层 wrap(固定视口), 不取 box——G6 渲染后会把 box 自身撑高
  // (121 节点实测 8440px), 二次 render 时 offsetHeight 已被污染
  const wrap = box.value.parentElement
  const W = (wrap && wrap.offsetWidth) || box.value.offsetWidth || 800
  const H = (wrap && wrap.offsetHeight) || 520

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
  } catch { /* 渲染中断(切页) */ }

  // G6 v5 + d3-force: 布局收敛/尺寸稳定后强制重绘一次(容器隐藏时首绘
  // 可能是空画布)。尺寸仍取外层 wrap, 不取被 G6 撑高的 box。
  setTimeout(() => {
    if (!graph || !box.value) return
    try {
      const wrap2 = box.value.parentElement
      graph.resize((wrap2 && wrap2.offsetWidth) || W, (wrap2 && wrap2.offsetHeight) || H)
      graph.render()
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
.sgt-page { display: flex; flex-direction: column; gap: 10px; height: calc(100vh - 220px); min-height: 480px; }
.sgt-toolbar { display: flex; align-items: center; gap: 10px; }
.sgt-meta { color: #86909c; font-size: 12px; margin-left: auto; }
.sgt-canvas-wrap {
  position: relative; flex: 1; border: 1px solid #eef1f7; border-radius: 10px;
  background: #fff; overflow: hidden;
}
.sgt-canvas { position: absolute; inset: 0; overflow: hidden; }
/* G6 v5 把 canvas 直接挂容器下且会按内容撑高(121 节点实测 8440px)——
   钳制 canvas 高度为容器高, 否则画布视口错位/被裁出可视区 */
.sgt-canvas canvas { position: absolute !important; top: 0 !important; left: 0 !important; }
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
</style>
