<template>
  <div class="kgc">
    <div class="kgc-head">
      <PartitionOutlined class="kgc-icon" />
      <span class="kgc-title">知识图谱 · {{ result.kb?.name || '检索结果' }}</span>
      <a-tag color="processing" class="kgc-tag">{{ nodes.length }} 实体</a-tag>
      <a-tag color="cyan" class="kgc-tag">{{ edges.length }} 关系</a-tag>
      <a-tag v-if="chunkHits" class="kgc-tag">{{ chunkHits }} 片段引用</a-tag>
    </div>

    <!-- 迷你力导图(G6 v5 动态导入,不进主包;悬停高亮一阶邻居) -->
    <div v-show="nodes.length" ref="box" class="kgc-chart" />

    <div v-if="!nodes.length" class="kgc-empty">
      未命中图谱实体(答案可能来自文档片段或"未找到")
    </div>

    <!-- 来源引用 -->
    <div v-if="result.sources?.entities?.length" class="kgc-sources">
      <span class="kgc-src-label"><LinkOutlined /> 引用实体</span>
      <a-tag v-for="e in result.sources.entities.slice(0, 12)" :key="e" class="kgc-chip">{{ e }}</a-tag>
      <a-tag v-if="result.sources.entities.length > 12" class="kgc-chip">
        +{{ result.sources.entities.length - 12 }}
      </a-tag>
    </div>
    <div v-if="result.sources?.chunks?.length" class="kgc-sources">
      <span class="kgc-src-label"><FileTextOutlined /> 文档片段</span>
      <a-tag v-for="(c, i) in result.sources.chunks.slice(0, 6)" :key="i" class="kgc-chip">
        {{ c.docName || '文档' }}{{ c.seq != null ? `#${c.seq}` : '' }}
      </a-tag>
    </div>
  </div>
</template>

<script setup lang="ts">
// 聊天前台的检索子图卡片:kb_search 返回的 kg_search_result 制品专用渲染。
// G6 v5 按需动态导入(参考 chat-bi SchemaGraph 的成熟用法)——只有出现
// 图谱结果时才加载 G6 chunk,主包零增量。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { FileTextOutlined, LinkOutlined, PartitionOutlined } from '@ant-design/icons-vue'

interface KgNode { id: string; name: string; type?: string; description?: string }
interface KgEdge { id?: string; source: string; target: string; type?: string; description?: string; evidence?: string }

const props = defineProps<{
  result: {
    type?: string
    kb?: { name?: string }
    subgraph?: { nodes?: KgNode[]; edges?: KgEdge[] }
    sources?: { entities?: string[]; chunks?: { docName?: string; seq?: number | null }[] }
  }
}>()

const nodes = computed<KgNode[]>(() => props.result.subgraph?.nodes || [])
const edges = computed<KgEdge[]>(() => props.result.subgraph?.edges || [])
const chunkHits = computed(() => props.result.sources?.chunks?.length || 0)

const box = ref<HTMLElement | null>(null)
let graph: import('@antv/g6').Graph | null = null
let resizeObserver: ResizeObserver | null = null

const _PALETTE = ['#5B8FF9', '#5AD8A6', '#F6BD16', '#E86452', '#6DC8EC',
                  '#945FB9', '#FF9845', '#1E9493', '#FF99C3', '#269A99']

function _esc(s: unknown): string {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string))
}

onMounted(async () => {
  if (!nodes.value.length || !box.value) return
  const G6 = await import('@antv/g6')
  if (!box.value) return  // 卸载竞态

  const types = Array.from(new Set(nodes.value.map((n) => n.type || '')))
  const degree: Record<string, number> = {}
  edges.value.forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  })
  const colorOf = (n: KgNode) => _PALETTE[types.indexOf(n.type || '') % _PALETTE.length]

  graph = new G6.Graph({
    container: box.value,
    width: box.value.offsetWidth || 560,
    height: 240,
    animation: false,
    data: {
      nodes: nodes.value.map((n) => ({
        id: n.id,
        data: { name: n.name, type: n.type || '', desc: n.description || '', color: colorOf(n), deg: degree[n.id] || 0 },
      })),
      edges: edges.value.map((e, i) => ({
        id: e.id || `e-${i}`, source: e.source, target: e.target,
        data: { type: e.type || '', desc: e.description || '', evidence: e.evidence || '' },
      })),
    },
    node: {
      style: {
        size: (d: any) => 14 + Math.min(20, (d.data?.deg || 0) * 4),
        fill: (d: any) => d.data?.color || '#94a3b8',
        stroke: '#fff', lineWidth: 2, cursor: 'pointer',
        labelText: (d: any) => d.data?.name || d.id,
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
    // preLayout: 布局先收敛再绘制,渲染后不抖(参考 chat-bi 踩坑结论)
    layout: {
      type: 'd3-force', preLayout: true, preventOverlap: true,
      linkDistance: 90, nodeStrength: -260, collideStrength: 0.8, alphaDecay: 0.05,
    },
    plugins: [{
      type: 'tooltip',
      getContent: (_e: unknown, items: { id: string; source?: string; data: Record<string, string> }[]) => {
        const it = items?.[0]
        if (!it) return '<div></div>'
        const d = it.data || {}
        const body = it.source !== undefined
          ? `<div><b>${_esc(d.type || '关系')}</b></div>${d.desc ? `<div>${_esc(d.desc)}</div>` : ''}${d.evidence ? `<div>「${_esc(d.evidence)}」</div>` : ''}`
          : `<div><b>${_esc(d.name)}</b></div><div>${_esc(d.type)}</div>${d.desc ? `<div>${_esc(d.desc)}</div>` : ''}`
        return `<div style="background:rgba(0,0,0,0.78);color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;line-height:1.6;max-width:280px;word-break:break-all">${body}</div>`
      },
    }],
    behaviors: [
      { type: 'drag-canvas' }, { type: 'zoom-canvas' }, { type: 'drag-element' },
      { type: 'hover-activate', degree: 1, state: 'highlight', inactiveState: 'dim',
        enable: (e: { targetType: string }) => e.targetType === 'node' },
    ],
  })

  await graph.render()
  await graph.fitView()
  // 大图 fitView 后过小时放大到可读比例(参考 chat-bi 的矫正逻辑)
  if (graph.getZoom() < 0.8) {
    graph.zoomTo(0.8)
    await graph.fitCenter()
  }
  resizeObserver = new ResizeObserver(() => {
    if (graph && box.value) graph.resize(box.value.offsetWidth, 240)
  })
  resizeObserver.observe(box.value)
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  graph?.destroy()
  graph = null
})
</script>

<style scoped>
.kgc { width: 100%; }
.kgc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kgc-icon { color: #2f54eb; }
.kgc-title { font-weight: 600; font-size: 13px; color: #1f2937; }
.kgc-tag { font-size: 11px; }
.kgc-chart { width: 100%; height: 240px; margin-top: 8px; border: 1px solid #eef1f6; border-radius: 10px; background: #fff; }
.kgc-empty { margin-top: 8px; font-size: 12px; color: #9ca3af; }
.kgc-sources { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.kgc-src-label { font-size: 12px; color: #6b7280; display: inline-flex; align-items: center; gap: 4px; }
.kgc-chip { font-size: 11px; }
</style>
