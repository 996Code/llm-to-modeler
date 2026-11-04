<template>
  <div class="ws">
    <div class="ws-bar">
      <a-input-search
        v-model:value="query" enter-button="联网搜索"
        placeholder="输入调研问题,如:JJG 544 压力控制器检定规程 最新版"
        style="max-width: 520px" :loading="loading" @search="run"
      />
      <a-select v-model:value="topK" style="width: 100px">
        <a-select-option :value="5">top 5</a-select-option>
        <a-select-option :value="8">top 8</a-select-option>
        <a-select-option :value="10">top 10</a-select-option>
      </a-select>
      <span class="ws-tip">经 SearXNG→google(代理出网);结果必带来源链接</span>
    </div>

    <!-- 最近调研记录(服务端落库,点击回填重跑) -->
    <div v-if="logs.length" class="ws-logs">
      <span class="ws-logs-label">最近调研:</span>
      <button v-for="l in logs.slice(0, 8)" :key="l.id" class="ws-logs-chip"
              :title="`${l.query} · ${l.resultCount} 条结果`"
              @click="query = l.query; run()">{{ l.query.slice(0, 20) }}</button>
      <a class="ws-logs-clear" @click="clearLogs">清空记录</a>
    </div>

    <template v-if="result">
      <section class="ws-card">
        <div class="ws-title">
          搜索结果
          <span class="ws-sub">「{{ result.query }}」共 {{ result.results.length }} 条</span>
          <a-tag v-if="result.cached" color="geekblue" class="ws-sub">缓存命中</a-tag>
        </div>
        <div v-if="result.results.length">
          <div v-for="(r, i) in result.results" :key="i" class="ws-item">
            <div class="ws-item-head">
              <span class="ws-item-idx">{{ i + 1 }}</span>
              <a :href="r.url" target="_blank" rel="noopener noreferrer" class="ws-item-title">{{ r.title || r.url }}</a>
            </div>
            <div v-if="r.content" class="ws-item-content">{{ r.content }}</div>
            <div class="ws-item-url">{{ r.url }}</div>
          </div>
        </div>
        <a-empty v-else description="未找到相关结果——换个搜索词试试" style="padding: 40px 0" />
      </section>
    </template>
    <a-empty
      v-else-if="!loading" description="输入问题调研外部信息;结果带来源链接,可点开核对"
      style="padding: 60px 0"
    />
  </div>
</template>

<script setup lang="ts">
import { inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { kgWebSearch, kgWebSearchLogs, kgWebSearchLogsClear, KgWebSearchResult, KgWebSearchLog } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const loadSafely = inject<LoadSafely>('loadSafely')!

const query = ref('')
const topK = ref(5)
const loading = ref(false)
const result = ref<KgWebSearchResult | null>(null)
const logs = ref<KgWebSearchLog[]>([])

async function refreshLogs() {
  try { logs.value = await kgWebSearchLogs() } catch { /* 记录加载失败不阻断调研 */ }
}

async function run() {
  const q = query.value.trim()
  if (!q) { message.warning('请输入搜索词'); return }
  if (loading.value) return   // 连点守卫:旧响应晚到不得覆盖新一轮
  loading.value = true
  await loadSafely(async () => {
    result.value = await kgWebSearch(q, topK.value)
    await refreshLogs()
  })
  loading.value = false
}

async function clearLogs() {
  await loadSafely(async () => {
    await kgWebSearchLogsClear()
    logs.value = []
    message.success('调研记录已清空')
  })
}

onMounted(refreshLogs)
</script>

<style scoped>
.ws-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }
.ws-bar > :deep(.ant-input-search) { flex: 1 1 280px; min-width: 0; }
.ws-tip { color: #94a3b8; font-size: 12px; }
.ws-logs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.ws-logs-label { font-size: 12px; color: #94a3b8; }
.ws-logs-chip {
  border: 1px solid #e2e8f0; border-radius: 8px; background: #fff;
  color: #64748b; font-size: 12px; padding: 2px 10px; cursor: pointer;
}
.ws-logs-chip:hover { border-color: var(--brand); color: var(--brand-ink); }
.ws-logs-clear { font-size: 12px; color: #cbd5e1; }
.ws-logs-clear:hover { color: #ef4444; }
.ws-card { border-top: 1px solid #eef1f7; padding: 16px 0 0; margin-top: 16px; background: #fff; }
.ws-title { font-weight: 600; font-size: 16px; color: #1f2937; margin-bottom: 12px; }
.ws-sub { color: #94a3b8; font-size: 12px; font-weight: 400; margin-left: 8px; }
.ws-item { padding: 10px 0; border-bottom: 1px dashed #eef1f7; }
.ws-item:last-child { border-bottom: none; }
.ws-item-head { display: flex; align-items: baseline; gap: 8px; }
.ws-item-idx { color: #cbd5e1; font-size: 12px; flex: none; }
.ws-item-title { color: #1f2937; font-weight: 500; text-decoration: none; }
.ws-item-title:hover { color: var(--brand); }
.ws-item-content { color: #6b7280; font-size: 13px; margin: 4px 0 0 20px; line-height: 1.6; }
.ws-item-url { color: #94a3b8; font-size: 12px; margin: 2px 0 0 20px; word-break: break-all; }
</style>
