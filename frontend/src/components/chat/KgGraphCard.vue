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
    <!-- 文档片段:手风琴列表——每条 chip 下方紧贴原文面板(展开/收起) -->
    <div v-if="result.sources?.chunks?.length" class="kgc-chunks">
      <span class="kgc-src-label"><FileTextOutlined /> 文档片段(点击查看原文)</span>
      <div v-for="(c, i) in result.sources.chunks" :key="i" class="kgc-chunk-item">
        <!-- 片段 chip(点击切换展开;手风琴:同一时间只展开一条)。
             编号"片段N"与回答文本里的 [片段N] 引用同源(sources.chunks
             顺序 = prompt loop.index),用户凭编号对照定位原文 -->
        <button class="kgc-chunk-trigger"
                :class="{ active: expandedChunk === i }"
                :disabled="!c.text"
                :title="c.text ? '点击查看片段原文' : '无原文'"
                @click="toggleChunk(i)">
          <span class="kgc-chunk-num">片段 {{ i + 1 }}</span>
          <span class="kgc-chunk-trigger-text">
            {{ c.docName || '文档' }}{{ c.seq != null ? ` #${c.seq}` : '' }}
          </span>
          <span v-if="c.score != null" class="kgc-score">{{ (Number(c.score)).toFixed(2) }}</span>
          <DownOutlined class="kgc-chunk-arrow" :class="{ expanded: expandedChunk === i }" />
        </button>
        <!-- 原文面板:紧贴 chip 下方,展开动画 -->
        <transition name="kgc-collapse">
          <div v-if="expandedChunk === i && c.text" class="kgc-chunk-text">
            <div class="kgc-chunk-head">
              <span>{{ c.docName || '文档' }}{{ c.seq != null ? ` · 块 ${c.seq}` : '' }}</span>
              <span v-if="c.score != null" class="kgc-score">相似度 {{ (Number(c.score)).toFixed(4) }}</span>
            </div>
            <div class="kgc-chunk-body">{{ c.text }}</div>
          </div>
        </transition>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 聊天前台的检索子图卡片:kb_search 返回的 kg_search_result 制品专用渲染。
// G6 v5 按需动态导入(参考 chat-bi SchemaGraph 的成熟用法)——只有出现
// 图谱结果时才加载 G6 chunk,主包零增量。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { DownOutlined, FileTextOutlined, LinkOutlined, PartitionOutlined } from '@ant-design/icons-vue'

interface KgNode { id: string; name: string; type?: string; description?: string }
interface KgEdge { id?: string; source: string; target: string; type?: string; description?: string; evidence?: string }
interface KgChunk { docId?: string; docName?: string; seq?: number | null; score?: number | null; text?: string }

const props = defineProps<{
  result: {
    type?: string
    kb?: { name?: string }
    subgraph?: { nodes?: KgNode[]; edges?: KgEdge[] }
    sources?: { entities?: string[]; chunks?: KgChunk[] }
  }
}>()

const nodes = computed<KgNode[]>(() => props.result.subgraph?.nodes || [])
const edges = computed<KgEdge[]>(() => props.result.subgraph?.edges || [])
const chunkHits = computed(() => props.result.sources?.chunks?.length || 0)

// 展开的片段下标(null=全收起);手风琴语义:点击已展开的收起,点击另一条切换
const expandedChunk = ref<number | null>(null)
function toggleChunk(i: number) {
  expandedChunk.value = expandedChunk.value === i ? null : i
}

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

  // 卸载窗口防护:动态 import 与 render 期间组件可能已销毁(graph 已
  // destroy/置空),后续调用会产生未捕获 rejection——纯噪音但污染控制台
  try {
    await graph.render()
    await graph.fitView()
    // 大图 fitView 后过小时放大到可读比例(参考 chat-bi 的矫正逻辑)
    if (graph && graph.getZoom() < 0.8) {
      graph.zoomTo(0.8)
      await graph.fitCenter()
    }
  } catch { /* 渲染中断(多为卸载竞态),静默 */ }
  if (!graph || !box.value) return
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

/* ===== 文档片段手风琴 ===== */
.kgc-chunks { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.kgc-chunk-item { display: flex; flex-direction: column; }
.kgc-chunk-trigger {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  border: 1px solid #eef1f6;
  border-radius: 8px;
  background: #fafbfc;
  cursor: pointer;
  font-size: 12px;
  color: #4b5563;
  transition: all 0.15s;
  text-align: left;
  width: 100%;
}
.kgc-chunk-trigger:hover:not(:disabled) { border-color: #c3d0f5; background: #f0f4ff; }
.kgc-chunk-trigger:disabled { cursor: default; opacity: 0.55; }
.kgc-chunk-trigger.active {
  border-color: #2f54eb;
  background: #eef2ff;
  color: #2f54eb;
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
}
/* 片段编号徽标(与回答文本里的 [片段N] 引用同色系,视觉对应) */
.kgc-chunk-num {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: 4px;
  background: rgba(47, 84, 235, 0.1);
  color: #2f54eb;
  font-size: 11px;
  font-weight: 500;
}
.kgc-chunk-trigger.active .kgc-chunk-num { background: #2f54eb; color: #fff; }
.kgc-chunk-trigger-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kgc-chunk-arrow { font-size: 10px; color: #9ca3af; transition: transform 0.2s; }
.kgc-chunk-arrow.expanded { transform: rotate(180deg); color: #2f54eb; }

/* 片段分数(相似度) */
.kgc-score { margin-left: 4px; opacity: 0.65; font-size: 10px; }

/* 展开的片段原文面板(紧贴 chip 下方) */
.kgc-chunk-text {
  border: 1px solid #2f54eb;
  border-top: none;
  border-radius: 0 0 8px 8px;
  background: #fff;
  overflow: hidden;
}
.kgc-chunk-head {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 12px;
  border-bottom: 1px solid #eef1f6;
  font-size: 12px; color: #6b7280;
}
.kgc-chunk-head > span:first-child { font-weight: 500; color: #374151; }
.kgc-chunk-body {
  padding: 10px 12px;
  font-size: 12.5px;
  line-height: 1.8;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}

/* 手风琴展开/收起过渡 */
.kgc-collapse-enter-active, .kgc-collapse-leave-active {
  transition: all 0.2s ease;
  max-height: 260px;
  overflow: hidden;
}
.kgc-collapse-enter-from, .kgc-collapse-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
