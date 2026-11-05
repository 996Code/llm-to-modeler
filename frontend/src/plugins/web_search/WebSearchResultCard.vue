<template>
  <div class="ws-result">
    <!-- 总结回答(DeepSeek/Perplexity 式):[来源:N] 渲染成角标徽标 -->
    <div class="md-content" v-html="renderedAnswer" @click="onRefClick" />
    <!-- 来源卡横滚(照 Perplexity Sources 模式:小卡片+域名+外链) -->
    <div v-if="results.length" class="ws-sources">
      <div class="ws-sources-head">
        <LinkOutlined class="ws-sources-icon" />
        <span>来源 · {{ results.length }} 个网页</span>
        <a-tag v-if="artifact.cached" color="geekblue" class="ws-cached-tag">缓存命中</a-tag>
      </div>
      <!-- 检索词规划(LLM 换角度改写,多路搜索的词) -->
      <div v-if="plannedQueries.length" class="ws-plan-row">
        <span class="ws-plan-label">检索词</span>
        <span v-for="(q, i) in plannedQueries" :key="i" class="ws-plan-chip">{{ q }}</span>
      </div>
      <div class="ws-sources-list">
        <a v-for="(r, i) in results" :key="i"
           class="ws-source-card" :href="r.url" target="_blank" rel="noopener noreferrer"
           :ref="el => sourceRefs[i] = el">
          <span class="ws-source-idx" :class="{ cited: citedSet.has(i + 1) }">{{ i + 1 }}</span>
          <span class="ws-source-title">{{ r.title || r.url }}</span>
          <span class="ws-source-domain">{{ domain(r.url) }}</span>
        </a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { LinkOutlined } from '@ant-design/icons-vue'
import { renderWebSearchAnswer } from './citations'

const props = defineProps<{
  artifact: Record<string, any>
  summary: string
  labels: Record<string, string>
  renderMarkdown: (text: string) => string
  compact: boolean
  citedChunks: number[]
}>()

const sourceRefs = ref<Record<number, Element | ComponentPublicInstance | null>>({})

const results = computed<any[]>(() => props.artifact?.results ?? [])
const plannedQueries = computed<string[]>(() => {
  const qs = props.artifact?.queries
  return Array.isArray(qs) ? qs.filter((q: unknown) => typeof q === 'string' && q) : []
})
const answer = computed<string>(() => props.artifact?.answer ?? '')

// 回答里的 [来源:N] 渲染成角标徽标(与 KG [片段:N] 同款交互形态)。
// 统一走 citations.renderWebSearchAnswer:只替换正文文本节点,不误伤
// 链接属性/锚文本/代码块;cited 即实际生效编号(非法编号不算)
const rendered = computed(() =>
  renderWebSearchAnswer(answer.value, results.value, props.renderMarkdown))
const renderedAnswer = computed(() => rendered.value.html)

// 被引用的来源编号(卡片序号高亮)
const citedSet = computed(() => rendered.value.cited)

function domain(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

// 角标点击:滚动到对应来源卡并闪一下(纯前端定位,无跳转)
function onRefClick(e: MouseEvent) {
  const el = (e.target as HTMLElement)
  const idx = Number(el.getAttribute('data-source-idx'))
  if (!el.classList.contains('md-ref-web') || !idx) return
  const card = sourceRefs.value[idx - 1]
  if (card) {
    (card as Element).scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
    ;(card as Element).classList.add('flash')
    setTimeout(() => (card as Element).classList.remove('flash'), 900)
  }
}
</script>

<style scoped>
.ws-result { min-width: 0; }
.md-content { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; margin-bottom: 12px; }
.md-content :deep(p) { margin: 0 0 8px; }
.md-content :deep(ul), .md-content :deep(ol) { padding-left: 20px; margin: 6px 0 12px; }
/* [来源:N] 角标徽标(蓝底,区别于 KG 紫色片段徽标) */
.md-content :deep(.md-ref-web) {
  display: inline-block; min-width: 18px; text-align: center;
  padding: 0 5px; margin: 0 2px; border-radius: 9px;
  background: var(--color-primary-bg, #eef4ff); color: var(--brand, #3370FF);
  font-size: 11px; font-weight: 600; line-height: 17px;
  vertical-align: baseline; cursor: pointer; user-select: none;
}
.md-content :deep(.md-ref-web:hover) { background: var(--brand, #3370FF); color: #fff; }

/* 来源卡纵向列表(编号+标题+域名一行式,与总结角标对应) */
.ws-sources { margin-top: 4px; }
.ws-sources-head {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: #94a3b8; margin-bottom: 8px;
}
.ws-sources-icon { font-size: 12px; }
.ws-cached-tag { margin-left: 4px; }
.ws-sources-list { display: flex; flex-direction: column; gap: 6px; }
.ws-plan-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.ws-plan-label { font-size: 11px; color: #94a3b8; flex: none; }
.ws-plan-chip {
  font-size: 11px; color: #64748b; background: #f1f5f9;
  border-radius: 8px; padding: 1px 8px; line-height: 18px;
}
.ws-source-card {
  display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  border: 1px solid #e2e8f0; border-radius: 8px; background: #fff;
  text-decoration: none; transition: all .15s;
}
.ws-source-card:hover { border-color: var(--brand, #3370FF); background: #fafbff; }
.ws-source-card.flash { border-color: var(--brand, #3370FF); background: var(--color-primary-bg, #eef4ff); }
.ws-source-idx {
  flex: none; min-width: 18px; text-align: center;
  padding: 0 4px; border-radius: 8px; font-size: 11px; font-weight: 600;
  line-height: 16px; background: #f1f5f9; color: #64748b;
}
.ws-source-idx.cited { background: var(--brand, #3370FF); color: #fff; }
.ws-source-title {
  flex: 1; min-width: 0; font-size: 12px; font-weight: 500; color: #1f2937;
  line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ws-source-domain { flex: none; font-size: 11px; color: #94a3b8; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
