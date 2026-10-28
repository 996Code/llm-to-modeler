<template>
  <div class="rt">
    <div class="rt-bar">
      <a-input-search
        v-model:value="query" :disabled="!kbId" enter-button="测试检索"
        placeholder="输入测试问题,如:张伟在哪个部门担任什么职务?"
        style="max-width: 480px" :loading="loading" @search="run"
      />
      <a-select v-model:value="topK" style="width: 100px">
        <a-select-option :value="5">top 5</a-select-option>
        <a-select-option :value="10">top 10</a-select-option>
        <a-select-option :value="20">top 20</a-select-option>
      </a-select>
      <span class="rt-tip">retrieve-only:只跑检索链,不做 LLM 综合回答</span>
    </div>

    <!-- D30 查询历史:最近 8 条留档(localStorage,跨会话),点击回填重跑 -->
    <div v-if="history.length" class="rt-history">
      <span class="rt-history-label">最近:</span>
      <button v-for="h in history" :key="h" class="rt-history-chip"
              :title="h" @click="query = h; run()">{{ h.slice(0, 18) }}</button>
      <a class="rt-history-clear" @click="clearHistory">清空</a>
    </div>

    <template v-if="result">
      <section class="rt-card">
        <div class="rt-title">检索意图</div>
        <template v-if="result.intent">
          <span class="rt-label">实体:</span>
          <a-tag v-for="e in result.intent.entities" :key="e" color="blue">{{ e }}</a-tag>
          <a-tag v-if="!result.intent.entities.length">无</a-tag>
          <span class="rt-label">关键词:</span>
          <a-tag v-for="k in result.intent.keywords" :key="k">{{ k }}</a-tag>
          <a-tag v-if="!result.intent.keywords.length">无</a-tag>
          <span class="rt-label">跳数:{{ result.intent.hop }}</span>
        </template>
        <a-tag v-else color="orange">意图解析失败(已降级)</a-tag>
      </section>

      <section class="rt-card">
        <div class="rt-title">图谱路 · 种子命中
          <span class="rt-sub">{{ result.diagnostics.termDetail.filter((t) => t.matched).length }}/{{ result.diagnostics.termDetail.length }} 词命中</span>
        </div>
        <a-table
          v-if="result.diagnostics.termDetail.length" size="small"
          :columns="termCols" :data-source="result.diagnostics.termDetail"
          :pagination="false" row-key="term"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'matched'">
              <a-tag v-if="record.matched" color="green">{{ record.matched }}<span v-if="record.type" class="rt-type"> · {{ record.type }}</span></a-tag>
              <a-tag v-else color="red">未命中</a-tag>
            </template>
          </template>
        </a-table>
        <div v-else class="rt-empty">意图无实体/关键词——图谱路未执行</div>
        <div class="rt-row">
          <span>子图:<b>{{ result.subgraph.nodes.length }}</b> 实体 / <b>{{ result.subgraph.edges.length }}</b> 关系</span>
          <a-tag v-if="result.diagnostics.nodesTruncated" color="orange">
            节点已达上限 {{ result.diagnostics.maxNodes }},子图被截断——召回可能不完整
          </a-tag>
          <a-tag v-if="result.diagnostics.edgesTruncated" color="orange">
            关系已达上限 {{ result.diagnostics.maxEdges }},被截断
          </a-tag>
          <a-tag v-if="!result.diagnostics.nodesTruncated && result.subgraph.nodes.length" color="green">未截断</a-tag>
        </div>
      </section>

      <section class="rt-card">
        <div class="rt-title">片段召回 · 向量路 + 全文兜底
          <a-tag v-if="(result.diagnostics.parentContextHits ?? 0) > 0" color="geekblue" class="rt-sub">
            {{ result.diagnostics.parentContextHits }} 段已回带父块上下文
          </a-tag>
        </div>
        <a-tag v-if="(result.diagnostics.docScopeFromEntity ?? []).length" color="purple">
          命中文档实体,已收敛到 {{ result.diagnostics.docScopeFromEntity!.length }} 份指定文档
        </a-tag>
        <a-tag v-if="result.diagnostics.docScopeRelaxed" color="orange">
          指定文档内零召回,已放开全库检索
        </a-tag>
        <a-tag v-if="result.diagnostics.vectorDegraded" color="red">向量检索失败,已降级纯图谱</a-tag>
        <a-tag v-if="(result.diagnostics.fulltextHits ?? 0) > 0" color="orange">
          图谱零命中,全文兜底救回 {{ result.diagnostics.fulltextHits }} 条
        </a-tag>
        <template v-if="result.chunks.length">
          <div v-for="(c, i) in result.chunks" :key="i" class="rt-chunk">
            <div class="rt-chunk-head">
              <a-tag color="purple">{{ c.docName || c.docId || '?' }}</a-tag>
              <a-tag v-if="c.source === 'fulltext'" color="orange">全文</a-tag>
              <a-tag v-if="c.parentContext" color="geekblue">已回带父块上下文</a-tag>
              <span v-if="c.seq !== undefined" class="rt-seq">#{{ c.seq }}</span>
              <span v-if="c.childSeq !== undefined && c.childSeq !== null" class="rt-seq">· 子段 {{ c.childSeq }}</span>
              <span v-if="c.parentChars" class="rt-seq">· 父块 {{ c.parentChars }} 字</span>
              <span class="rt-score">{{ (c.score ?? 0).toFixed(4) }}</span>
            </div>
            <div class="rt-chunk-text" :class="{ expanded: expandedChunks.has(i) }">{{ c.text }}</div>
            <a v-if="(c.text || '').length > 120" class="rt-expand" @click="toggleChunk(i)">
              {{ expandedChunks.has(i) ? '收起' : '展开全文(回带上下文)' }}
            </a>
          </div>
        </template>
        <div v-else class="rt-empty">无命中(向量未启用/零召回且全文兜底无结果)</div>
      </section>
    </template>

    <a-empty
      v-else-if="!loading" description="输入问题跑一次检索,看三路召回明细与水位"
      style="padding: 60px 0"
    />
  </div>
</template>

<script setup lang="ts">
import { inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { kgRetrieveTest, KgRetrieveTestResult } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const query = ref('')
const topK = ref(5)
const loading = ref(false)
const result = ref<KgRetrieveTestResult | null>(null)
// 片段展开态(回带窗口长文本默认折叠,点击展开)
const expandedChunks = ref(new Set<number>())

function toggleChunk(i: number) {
  const s = new Set(expandedChunks.value)
  s.has(i) ? s.delete(i) : s.add(i)
  expandedChunks.value = s
}

// 切库即弃旧结果:诊断页残留旧库数据 = 误诊(兄弟组件 DocManager/GraphView 同款 watch 惯例)
watch(() => props.kbId, () => { result.value = null; expandedChunks.value = new Set() })

const termCols = [
  { title: '意图词', dataIndex: 'term', key: 'term', width: 160 },
  { title: '命中实体', key: 'matched' },
]

async function run() {
  const q = query.value.trim()
  if (!q) { message.warning('请输入测试问题'); return }
  if (loading.value) return   // 连点守卫:旧响应晚到不得覆盖新一轮
  pushHistory(q)
  loading.value = true
  await loadSafely(async () => {
    result.value = await kgRetrieveTest(props.kbId, q, topK.value)
    expandedChunks.value = new Set()
  })
  loading.value = false
}

// ── D30 查询历史(localStorage 留档,最近 8 条去重) ─────────────
const HISTORY_KEY = 'kg_retrieve_history'
const history = ref<string[]>(JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'))

function pushHistory(q: string) {
  const next = [q, ...history.value.filter(h => h !== q)].slice(0, 8)
  history.value = next
  localStorage.setItem(HISTORY_KEY, JSON.stringify(next))
}

function clearHistory() {
  history.value = []
  localStorage.removeItem(HISTORY_KEY)
}
</script>

<style scoped>
.rt-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }
.rt-bar > :deep(.ant-input-search) { flex: 1 1 240px; min-width: 0; }
/* D30 查询历史 chips */
.rt-history { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.rt-history-label { font-size: 12px; color: #94a3b8; }
.rt-history-chip {
  border: 1px solid #e2e8f0; border-radius: 8px; background: #fff;
  color: #64748b; font-size: 12px; padding: 2px 10px; cursor: pointer;
}
.rt-history-chip:hover { border-color: #2f54eb; color: #2f54eb; }
.rt-history-clear { font-size: 12px; color: #cbd5e1; }
.rt-history-clear:hover { color: #ef4444; }
.rt-tip { color: #94a3b8; font-size: 12px; }
.rt-card { border-top: 1px solid #eef1f7; padding: 16px 0 0; margin-top: 16px; background: #fff; }
.rt-title { font-weight: 600; font-size: 16px; color: #1f2937; margin-bottom: 8px; }
.rt-sub { color: #94a3b8; font-size: 12px; font-weight: 400; margin-left: 8px; }
.rt-label { color: #6b7280; font-size: 12px; margin: 0 4px 0 8px; }
.rt-row { margin-top: 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: #6b7280; }
.rt-chunk { border: 1px solid #f0f0f0; border-radius: 8px; padding: 8px 12px; margin-top: 8px; }
.rt-chunk-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.rt-seq { color: #94a3b8; font-size: 12px; }
.rt-score { margin-left: auto; color: #7c3aed; font-weight: 600; font-family: monospace; }
.rt-chunk-text { color: #4b5563; font-size: 12px; margin-top: 4px; white-space: pre-wrap; max-height: 96px; overflow: hidden; }
.rt-chunk-text.expanded { max-height: none; overflow: visible; }
.rt-expand { display: inline-block; margin-top: 4px; font-size: 12px; color: #6366f1; cursor: pointer; }
.rt-empty { color: #9ca3af; font-size: 12px; }
.rt-type { color: #6b7280; font-weight: 400; }
</style>
