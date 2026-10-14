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

    <a-alert
      v-if="error" type="error" show-icon style="margin-top: 12px"
      :message="`检索测试失败:${error}`"
    />

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
        <div class="rt-title">向量路 · 片段召回</div>
        <a-tag v-if="result.diagnostics.vectorDegraded" color="red">向量检索失败,已降级纯图谱</a-tag>
        <template v-if="result.chunks.length">
          <div v-for="(c, i) in result.chunks" :key="i" class="rt-chunk">
            <div class="rt-chunk-head">
              <a-tag color="purple">{{ c.docName || c.docId || '?' }}</a-tag>
              <span v-if="c.seq !== undefined" class="rt-seq">#{{ c.seq }}</span>
              <span class="rt-score">{{ (c.score ?? 0).toFixed(4) }}</span>
            </div>
            <div class="rt-chunk-text">{{ c.text }}</div>
          </div>
        </template>
        <div v-else class="rt-empty">无命中(库未启用向量或零召回)</div>
      </section>
    </template>

    <a-empty
      v-else-if="!loading" description="输入问题跑一次检索,看两路召回明细与水位"
      style="padding: 60px 0"
    />
  </div>
</template>

<script setup lang="ts">
import { inject, ref } from 'vue'
import { message } from 'ant-design-vue'
import { kgRetrieveTest, KgRetrieveTestResult } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const query = ref('')
const topK = ref(5)
const loading = ref(false)
const error = ref('')
const result = ref<KgRetrieveTestResult | null>(null)

const termCols = [
  { title: '意图词', dataIndex: 'term', key: 'term', width: 160 },
  { title: '命中实体', key: 'matched' },
]

async function run() {
  const q = query.value.trim()
  if (!q) { message.warning('请输入测试问题'); return }
  loading.value = true
  error.value = ''
  await loadSafely(async () => {
    result.value = await kgRetrieveTest(props.kbId, q, topK.value)
  })
  loading.value = false
}
</script>

<style scoped>
.rt-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.rt-tip { color: #94a3b8; font-size: 12px; }
.rt-card { border: 1px solid #eef1f7; border-radius: 10px; padding: 12px 16px; margin-top: 12px; background: #fff; }
.rt-title { font-weight: 600; font-size: 13.5px; color: #1f2937; margin-bottom: 8px; }
.rt-sub { color: #94a3b8; font-size: 12px; font-weight: 400; margin-left: 8px; }
.rt-label { color: #6b7280; font-size: 12.5px; margin: 0 4px 0 8px; }
.rt-row { margin-top: 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12.5px; color: #6b7280; }
.rt-chunk { border: 1px solid #f0f0f0; border-radius: 8px; padding: 8px 12px; margin-top: 8px; }
.rt-chunk-head { display: flex; align-items: center; gap: 8px; }
.rt-seq { color: #94a3b8; font-size: 12px; }
.rt-score { margin-left: auto; color: #7c3aed; font-weight: 600; font-family: monospace; }
.rt-chunk-text { color: #4b5563; font-size: 12.5px; margin-top: 4px; white-space: pre-wrap; max-height: 96px; overflow: hidden; }
.rt-empty { color: #9ca3af; font-size: 12.5px; }
.rt-type { color: #6b7280; font-weight: 400; }
</style>
