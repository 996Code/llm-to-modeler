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

    <!-- 最近调研记录(服务端落库,点击直接回看该条已存结果,不重搜) -->
    <div v-if="logs.length" class="ws-logs">
      <span class="ws-logs-label">最近调研:</span>
      <button v-for="l in logs.slice(0, 8)" :key="l.id" class="ws-logs-chip"
              :title="`${l.query} · ${l.resultCount} 条结果`"
              @click="viewLog(l)">{{ l.query.slice(0, 20) }}</button>
      <a class="ws-logs-clear" @click="clearLogs">清空记录</a>
    </div>

    <template v-if="result">
      <!-- 总结回答(LLM 综合归纳,DeepSeek/Perplexity 式) -->
      <section v-if="result.answer" class="ws-card">
        <div class="ws-title">总结回答
          <a-tag v-if="result.cached" color="geekblue" class="ws-sub">缓存命中</a-tag>
        </div>
        <div class="ws-answer md-content" v-html="renderedAnswer" @click="onRefClick" />
      </section>
      <!-- 来源卡(角标编号与总结里的 [来源:N] 对应) -->
      <section v-if="result.results.length" class="ws-card">
        <div class="ws-title">来源 · {{ result.results.length }} 个网页</div>
        <div ref="itemsEl" class="ws-items">
          <a v-for="(r, i) in result.results" :key="i"
             class="ws-item" :href="r.url" target="_blank" rel="noopener noreferrer">
            <span class="ws-item-idx" :class="{ cited: citedSet.has(i + 1) }">{{ i + 1 }}</span>
            <div class="ws-item-main">
              <div class="ws-item-title">{{ r.title || r.url }}</div>
              <div v-if="r.content" class="ws-item-content">{{ r.content }}</div>
              <div class="ws-item-url">{{ domain(r.url) }}</div>
            </div>
          </a>
        </div>
      </section>
      <a-empty v-else-if="!result.answer" description="未找到相关结果——换个搜索词试试" style="padding: 40px 0" />
    </template>
    <a-empty
      v-else-if="!loading" description="输入问题调研外部信息;结果带来源链接,可点开核对"
      style="padding: 60px 0"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { marked } from 'marked'
import { kgWebSearch, kgWebSearchLogs, kgWebSearchLogsClear, KgWebSearchResult, KgWebSearchLog } from '../../api'
import { renderWebSearchAnswer } from '../../../plugins/web_search/citations'
import type { LoadSafely } from '../../components/loadSafely'

const loadSafely = inject<LoadSafely>('loadSafely')!

const query = ref('')
const topK = ref(5)
const loading = ref(false)
const result = ref<KgWebSearchResult | null>(null)
const logs = ref<KgWebSearchLog[]>([])
const itemsEl = ref<HTMLElement | null>(null)

// 总结回答渲染:[来源:N] → 角标徽标(与对话端 WebSearchResultCard 同款形态)。
// 统一走 citations.renderWebSearchAnswer:只替换正文文本节点里的 [来源:N],
// 不误伤链接属性/锚文本/代码块,编号合法性(来源 URL 须 http/https)也在内
const renderedAnswer = computed(() => rendered.value.html)
const citedSet = computed(() => rendered.value.cited)
const rendered = computed(() => renderWebSearchAnswer(
  result.value?.answer || '',
  result.value?.results || [],
  raw => marked.parse(raw) as string,
))
function domain(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

// 角标点击:滚动到对应来源卡(纯前端定位,无跳转;与 WebSearchResultCard 同款)
function onRefClick(e: MouseEvent) {
  const el = e.target as HTMLElement
  if (!el.classList.contains('md-ref-web')) return
  const idx = Number(el.getAttribute('data-source-idx'))
  if (!idx) return
  itemsEl.value?.querySelectorAll('.ws-item')[idx - 1]
    ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}

async function refreshLogs() {
  try { logs.value = await kgWebSearchLogs() } catch { /* 记录加载失败不阻断调研 */ }
}

// 历史回看:直接展示该条已存的 results+answer(不重搜、不耗出站;
// 旧记录无 answer 时只展示来源卡)
function viewLog(l: KgWebSearchLog) {
  query.value = l.query
  result.value = {
    query: l.query, results: l.results, cached: l.cached,
    answer: l.answer, logId: l.id,
  }
}

async function run() {
  const q = query.value.trim()
  if (!q) { message.warning('请输入搜索词'); return }
  if (loading.value) return   // 连点守卫:旧响应晚到不得覆盖新一轮
  loading.value = true
  await loadSafely(async () => {
    result.value = await kgWebSearch(q, topK.value)
    if (result.value.logError) message.warning(result.value.logError)
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
/* 总结回答(markdown)+角标徽标 */
.ws-answer { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.ws-answer :deep(p) { margin: 0 0 8px; }
.ws-answer :deep(ul), .ws-answer :deep(ol) { padding-left: 20px; margin: 6px 0 12px; }
.md-ref-web {
  display: inline-block; min-width: 18px; text-align: center;
  padding: 0 5px; margin: 0 2px; border-radius: 9px;
  background: var(--color-primary-bg, #eef4ff); color: var(--brand, #3370FF);
  font-size: 11px; font-weight: 600; line-height: 17px; vertical-align: baseline;
  cursor: pointer; user-select: none;
}
.md-ref-web:hover { background: var(--brand, #3370FF); color: #fff; }
/* 来源卡列表(编号与角标对应,被引用高亮) */
.ws-items { display: flex; flex-direction: column; gap: 8px; }
.ws-item {
  display: flex; gap: 10px; padding: 10px 12px;
  border: 1px solid #e2e8f0; border-radius: 8px; background: #fff;
  text-decoration: none; transition: all .15s;
}
.ws-item:hover { border-color: var(--brand); box-shadow: 0 2px 6px rgba(51,112,255,.12); }
.ws-item-idx {
  flex: none; min-width: 18px; text-align: center; margin-top: 2px;
  padding: 0 4px; border-radius: 8px; font-size: 11px; font-weight: 600;
  line-height: 16px; background: #f1f5f9; color: #64748b;
}
.ws-item-idx.cited { background: var(--brand, #3370FF); color: #fff; }
.ws-item-main { min-width: 0; flex: 1; }
.ws-item-title { color: #1f2937; font-weight: 500; font-size: 13px; line-height: 1.4; }
.ws-item-content { color: #6b7280; font-size: 12px; margin-top: 3px; line-height: 1.6;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ws-item-url { color: #94a3b8; font-size: 11px; margin-top: 3px; }
</style>
