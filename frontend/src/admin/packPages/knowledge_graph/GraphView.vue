<template>
  <div class="gv-page">
    <!-- 工具条:搜索 / 类型过滤 / 重置 -->
    <div class="gv-toolbar">
      <a-input-search
        v-model:value="filterQ" placeholder="按实体名/描述过滤" style="width: 240px"
        allow-clear @search="reload"
      />
      <a-select
        v-model:value="filterTypes" mode="multiple" style="min-width: 220px"
        placeholder="实体类型(全部)" allow-clear @change="reload"
      >
        <a-select-option v-for="t in typeOptions" :key="t" :value="t">
          <span class="gv-dot" :style="{ background: colorOf(t) }" /> {{ t }}
        </a-select-option>
      </a-select>
      <a-button @click="reload"><SyncOutlined :spin="loading" /> 重新加载</a-button>
      <a-button :disabled="!dirty" @click="resetView"><UndoOutlined /> 重置视图</a-button>
      <a-divider type="vertical" />
      <!-- 缩放控制(chat-bi 同款交互):放大/缩小/适应画布 -->
      <a-button-group size="small">
        <a-button :disabled="!graphReady" @click="zoomBy(1.25)" title="放大"><ZoomInOutlined /></a-button>
        <a-button :disabled="!graphReady" @click="zoomBy(0.8)" title="缩小"><ZoomOutOutlined /></a-button>
        <a-button :disabled="!graphReady" @click="fitAll" title="适应画布"><ExpandOutlined /></a-button>
      </a-button-group>
      <span class="gv-tip">
        {{ nodes.length }} 节点 / {{ edges.length }} 边 · 单击看详情,双击展开邻居,悬停高亮一阶邻域
      </span>
      <!-- 展开进行中提示:画布上双击没有按钮 loading 态,这里给"正在展开"感知 -->
      <span v-if="expanding" class="gv-expanding"><LoadingOutlined spin /> 正在展开邻居…</span>
    </div>

    <!-- 画布:G6 v5。加载态用 visibility 而非 v-if/display:none ——
         G6 mount 时必须能量到容器尺寸,display:none 下初始化的图不渲染
         (chat-bi 踩坑结论,实测画布全白) -->
    <div ref="chartBox" class="gv-chart" :class="{ 'gv-hidden': loading }" />
    <div v-if="!loading && !nodes.length" class="gv-empty">
      <a-empty description="暂无图谱数据(先在「文档导入」页导入文档)" />
    </div>

    <!-- 节点/边 详情抽屉 -->
    <a-drawer v-model:open="detailOpen" width="460" :title="detailTitle">
      <template v-if="nodeDetail">
        <a-descriptions :column="1" size="small" bordered>
          <a-descriptions-item label="名称">{{ nodeDetail.name }}</a-descriptions-item>
          <a-descriptions-item label="类型">
            <a-tag :color="colorOf(nodeDetail.type)">{{ nodeDetail.type || '未知' }}</a-tag>
            <a-tag v-if="nodeDetail.typeStatus === 'proposed'" color="orange">类型待审</a-tag>
          </a-descriptions-item>
          <a-descriptions-item v-if="nodeDetail.description" label="描述">{{ nodeDetail.description }}</a-descriptions-item>
          <a-descriptions-item v-if="nodeDetail.aliases?.length" label="别名">{{ nodeDetail.aliases.join('、') }}</a-descriptions-item>
        </a-descriptions>
        <div style="margin-top: 14px; display: flex; gap: 10px">
          <a-button type="primary" :loading="expanding" @click="expandNode(nodeDetail)">
            <ExpandOutlined /> 展开 1 跳邻居
          </a-button>
        </div>
      </template>
      <template v-else-if="edgeDetail">
        <a-descriptions :column="1" size="small" bordered>
          <a-descriptions-item label="关系">{{ edgeDetail.type }}</a-descriptions-item>
          <a-descriptions-item label="源 → 目标">
            {{ nodeName(edgeDetail.source) }} → {{ nodeName(edgeDetail.target) }}
          </a-descriptions-item>
          <a-descriptions-item v-if="edgeDetail.description" label="说明">{{ edgeDetail.description }}</a-descriptions-item>
          <a-descriptions-item v-if="edgeDetail.evidence" label="原文证据">
            <span class="gv-evidence">「{{ edgeDetail.evidence }}」</span>
          </a-descriptions-item>
        </a-descriptions>
      </template>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 图谱浏览(G6 v5 版,参考 chat-bi SchemaGraph 的成熟配置):
// d3-force preLayout(渲染后不抖)/ hover-activate 一阶邻域高亮+dim /
// 单击详情抽屉 / 双击展开邻居(增量 merge) / minimap / fitView 焦点矫正 /
// tooltip 黑底详情 / ResizeObserver 自适应。
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  ExpandOutlined, LoadingOutlined, SyncOutlined, UndoOutlined, ZoomInOutlined, ZoomOutOutlined,
} from '@ant-design/icons-vue'
import {
  KgGraphData, KgGraphNode, KgGraphEdge, KgSchema,
  expandKgNode, fetchKgGraph,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string; schema: KgSchema | null; refreshTick?: number }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const chartBox = ref<HTMLElement | null>(null)
let graph: import('@antv/g6').Graph | null = null
let resizeObserver: ResizeObserver | null = null
const graphReady = ref(false)

/** 缩放控制(以画布中心为锚) */
async function zoomBy(factor: number) {
  if (!graph) return
  const z = graph.getZoom()
  await graph.zoomTo(Math.min(4, Math.max(0.15, z * factor)))
}
/** 适应画布 + 可读性矫正(过小时放大,chat-bi 同款) */
async function fitAll() {
  if (!graph) return
  await graph.fitView()
  if (graph.getZoom() < 0.8) {
    graph.zoomTo(0.8)
    await graph.fitCenter()
  }
}

const loading = ref(false)
const dirty = ref(false)
const expanding = ref(false)
let clickTimer: number | undefined   // 单击/双击消歧(见 node:click 注释)
const filterQ = ref('')
const filterTypes = ref<string[]>([])
const nodes = ref<KgGraphNode[]>([])
const edges = ref<KgGraphEdge[]>([])
let loadSeq = 0        // 请求序号守卫(切库错序防护)

const nodeDetail = ref<KgGraphNode | null>(null)
const edgeDetail = ref<KgGraphEdge | null>(null)
const detailOpen = ref(false)
const detailTitle = computed(() => nodeDetail.value ? `实体:${nodeDetail.value.name}` : '关系详情')

// 类型 → 颜色(本体声明色 > AntV 调色板)
const _PALETTE = ['#5B8FF9', '#5AD8A6', '#F6BD16', '#E86452', '#6DC8EC',
                  '#945FB9', '#FF9845', '#1E9493', '#FF99C3', '#269A99']
const typeOptions = computed(() => Array.from(new Set(nodes.value.map((n) => n.type).filter(Boolean))))
function colorOf(type: string): string {
  const ets = props.schema?.entity_types || []
  const declared = ets.find((t) => t.key === type)
  if (declared?.color) return declared.color
  const idx = typeOptions.value.indexOf(type)
  return _PALETTE[(idx >= 0 ? idx : type.length) % _PALETTE.length]
}

function nodeName(id: string): string {
  const found = nodes.value.find((n) => n.id === id)
  if (found) return found.name
  const idx = id.lastIndexOf(':')
  return idx >= 0 ? id.slice(idx + 1) : id.slice(-24)
}

function _esc(s: unknown): string {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string))
}

// ── 数据加载与渲染 ──────────────────────────────────────

async function reload() {
  // 请求序号守卫:快速切换知识库时,旧库的晚到响应不得渲染到新库画布
  const myKb = props.kbId
  const seq = ++loadSeq
  loading.value = true
  await loadSafely(async () => {
    const data = await fetchKgGraph(myKb, {
      q: filterQ.value || undefined,
      types: filterTypes.value.length ? filterTypes.value.join(',') : undefined,
    })
    if (seq !== loadSeq || props.kbId !== myKb) return
    nodes.value = data.nodes
    edges.value = data.edges
    dirty.value = false
    await render(true)
  })
  if (seq === loadSeq) loading.value = false
}

async function expandNode(node: KgGraphNode) {
  // 并发守卫:双击连点/抽屉按钮与双击并发时,两次请求都基于同一份旧
  // nodes 去重再各自 merge → 同一节点 ID 出现两份,G6 setData 渲染异常
  if (expanding.value) return
  expanding.value = true
  try {
    await loadSafely(async () => {
      const add = await expandKgNode(props.kbId, node.id)
      const knownIds = new Set(nodes.value.map((n) => n.id))
      const added = add.nodes.filter((n) => !knownIds.has(n.id))
      const knownEdgeIds = new Set(edges.value.map((e) => e.id))
      const addedEdges = add.edges.filter((e) => !knownEdgeIds.has(e.id) &&
        add.nodes.some((n) => n.id === e.source) && add.nodes.some((n) => n.id === e.target))
      nodes.value = [...nodes.value, ...added]
      edges.value = [...edges.value, ...addedEdges]
      dirty.value = true
      await render(false)  // 增量重渲染不 fitView(保持用户当前视角)
      // 新邻居高亮一下,让"展开了什么"可感知
      graph?.setElementState(node.id, ['selected'])
      added.forEach((n) => graph?.setElementState(n.id, ['selected']))
      message.success(`展开:新增 ${added.length} 节点 / ${addedEdges.length} 边`)
    })
  } finally {
    expanding.value = false
  }
}

function resetView() {
  reload()
}

async function render(fit: boolean) {
  const G6 = await import('@antv/g6')
  if (!chartBox.value) return

  const types = typeOptions.value
  const degree: Record<string, number> = {}
  edges.value.forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  })

  const data = {
    nodes: nodes.value.map((n) => ({
      id: n.id,
      data: {
        name: n.name, type: n.type || '', desc: n.description || '',
        aliases: n.aliases || [], typeStatus: n.typeStatus || '', deg: degree[n.id] || 0,
        color: colorOf(n.type),
      },
    })),
    edges: edges.value.map((e, i) => ({
      id: e.id || `e-${i}`, source: e.source, target: e.target,
      data: { type: e.type || '', desc: e.description || '', evidence: e.evidence || '' },
    })),
  }

  if (!graph) {
    graph = new G6.Graph({
      container: chartBox.value,
      width: chartBox.value.offsetWidth || 1000,
      height: chartBox.value.offsetHeight || 500,
      animation: false,
      data,
      node: {
        style: {
          size: (d: any) => 16 + Math.min(28, (d.data?.deg || 0) * 4),
          fill: (d: any) => d.data?.color || '#94a3b8',
          stroke: '#fff', lineWidth: 2, cursor: 'pointer',
          labelText: (d: any) => d.data?.name || d.id,
          labelFontSize: 11, labelFill: '#374151', labelPlacement: 'right',
          // 枢纽节点光晕(连接数高的实体)
          shadowColor: (d: any) => ((d.data?.deg || 0) >= 4 ? d.data?.color : 'transparent'),
          shadowBlur: (d: any) => ((d.data?.deg || 0) >= 4 ? 14 : 0),
        },
        state: {
          selected: { stroke: '#1890ff', lineWidth: 3, shadowColor: 'rgba(24,144,255,0.5)', shadowBlur: 16 },
          highlight: { stroke: '#D580FF', lineWidth: 3, shadowColor: 'rgba(213,128,255,0.45)', shadowBlur: 12 },
          // G6 v5 坑(参考 chat-bi 注释):dim 态 opacity 可能不触发重绘,用 fillOpacity
          dim: { fillOpacity: 0.25, strokeOpacity: 0.25, labelOpacity: 0.25 },
        },
      },
      edge: {
        type: 'quadratic',  // 曲线边,平行边不重叠
        style: {
          stroke: '#94a3b8', lineWidth: 1.2, endArrow: true, endArrowSize: 6, cursor: 'pointer',
          labelText: (d: any) => d.data?.type || '',
          labelFontSize: 9, labelFill: '#64748b',
          labelBackground: true, labelBackgroundFill: '#fff',
          labelBackgroundOpacity: 0.85, labelBackgroundRadius: 2,
          labelBackgroundPadding: [1, 3, 1, 3],
        },
        state: {
          selected: { stroke: '#1890ff', lineWidth: 2.5 },
          highlight: { stroke: '#D580FF', lineWidth: 2 },
          dim: { strokeOpacity: 0.12, labelOpacity: 0.12 },
        },
      },
      layout: {
        type: 'd3-force', preLayout: true, preventOverlap: true,
        // 链距/斥力偏紧凑:小图(几十节点)不至于散成"看不清的点"
        linkDistance: (d: unknown) => 60 + Math.random() * 60,
        nodeStrength: -180, edgeStrength: 0.1,
        collideStrength: 0.9, alphaDecay: 0.05, alphaMin: 0.001,
      },
      plugins: [
        { type: 'minimap', size: [160, 100], position: 'right-bottom' },
        {
          type: 'tooltip',
          getContent: (_e: unknown, items: { id: string; source?: string; data: Record<string, string> }[]) => {
            const it = items?.[0]
            if (!it) return '<div></div>'
            const d = it.data || {}
            const body = it.source !== undefined
              ? `<div><b>${_esc(d.type || '关系')}</b></div>${d.desc ? `<div>${_esc(d.desc)}</div>` : ''}${d.evidence ? `<div>「${_esc(d.evidence)}」</div>` : ''}`
              : `<div><b>${_esc(d.name)}</b> <span style="opacity:.7">${_esc(d.type)}</span></div>${d.desc ? `<div>${_esc(d.desc)}</div>` : ''}<div style="opacity:.7">连接 ${_esc(d.deg)} · 双击展开邻居</div>`
            return `<div style="background:rgba(0,0,0,0.78);color:#fff;padding:8px 12px;border-radius:6px;font-size:12px;line-height:1.6;max-width:300px;word-break:break-all">${body}</div>`
          },
        },
      ],
      behaviors: [
        { type: 'drag-canvas' }, { type: 'zoom-canvas' }, { type: 'drag-element' },
        {
          type: 'hover-activate', degree: 1, state: 'highlight', inactiveState: 'dim',
          enable: (e: { targetType: string }) => e.targetType === 'node',
        },
      ],
    })

    // 单击/双击消歧:单击延迟 260ms 开抽屉;双击的第二击会先到(清掉挂起
    // 的开抽屉定时器),让 node:dblclick 接管展开。否则第一次单击立即挂上
    // 详情抽屉的全屏 mask,双击的第二击落在 mask 上把抽屉关掉——
    // node:dblclick 永远收不到,画布双击展开形同虚设。
    graph.on('node:click', (e: any) => {
      const id = e.target?.id
      const n = nodes.value.find((x) => x.id === id)
      if (!n) return
      if (clickTimer) { window.clearTimeout(clickTimer); clickTimer = undefined; return }
      clickTimer = window.setTimeout(() => {
        clickTimer = undefined
        nodeDetail.value = n; edgeDetail.value = null; detailOpen.value = true
      }, 260)
    })
    graph.on('node:dblclick', (e: any) => {
      if (clickTimer) { window.clearTimeout(clickTimer); clickTimer = undefined }
      const id = e.target?.id
      const n = nodes.value.find((x) => x.id === id)
      if (n) { detailOpen.value = false; expandNode(n) }
    })
    graph.on('edge:click', (e: any) => {
      const id = e.target?.id
      const edge = edges.value.find((x) => x.id === id)
      if (edge) {
        if (clickTimer) { window.clearTimeout(clickTimer); clickTimer = undefined }
        edgeDetail.value = edge; nodeDetail.value = null; detailOpen.value = true
      }
    })

    await graph.render()
    graphReady.value = true
    resizeObserver = new ResizeObserver(() => {
      if (graph && chartBox.value) {
        // 钳制:异常超大尺寸直接忽略(防反馈循环打满 canvas 上限)
        const w = chartBox.value.offsetWidth, h = chartBox.value.offsetHeight
        if (w > 0 && h > 0 && w < 5000 && h < 5000) graph.resize(w, h)
      }
    })
    resizeObserver.observe(chartBox.value)
  } else {
    graph.setData(data)
    await graph.render()
  }

  if (fit) await fitAll()
}

watch(() => props.kbId, () => { filterQ.value = ''; filterTypes.value = []; reload() })
watch(() => props.refreshTick, () => { reload() })
onMounted(reload)
onBeforeUnmount(() => {
  if (clickTimer) { window.clearTimeout(clickTimer); clickTimer = undefined }
  resizeObserver?.disconnect()
  graph?.destroy()
  graph = null
  graphReady.value = false
})
</script>

<style scoped>
.gv-page { display: flex; flex-direction: column; }
.gv-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.gv-tip { color: #9ca3af; font-size: 12px; margin-left: auto; }
.gv-expanding { color: #2563eb; font-size: 12px; white-space: nowrap; }
/* 显式高度(不用 flex:1):G6 canvas 会反向撑大容器,flex 高度构成
   "canvas 变大 → 容器变大 → resize 更大" 的失控反馈(实测打满 2^24) */
.gv-chart {
  height: calc(100vh - 330px); min-height: 420px; max-height: 900px;
  border: 1px solid #e6ebf2; border-radius: 12px; background: #fff; overflow: hidden;
}
.gv-hidden { visibility: hidden; }
.gv-empty { height: 420px; display: flex; align-items: center; justify-content: center; }
.gv-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.gv-evidence { color: #2f54eb; }
</style>
