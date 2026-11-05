<template>
  <div class="kg-result">
    <div class="md-content" v-html="renderMarkdown(artifact.answer)" />
    <KgGraphCard :result="artifact" :compact="compact" :cited-chunks="citedChunks" />
  </div>
</template>

<script setup lang="ts">
import KgGraphCard from '../../components/chat/KgGraphCard.vue'

defineProps<{
  artifact: Record<string, any>
  summary: string
  labels: Record<string, string>
  renderMarkdown: (text: string) => string
  compact: boolean
  citedChunks: number[]
}>()
</script>

<style scoped>
.kg-result { min-width: 0; }
.md-content { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; margin-bottom: 12px; }
.md-content :deep(p) { margin: 0 0 8px; }
.md-content :deep(ul), .md-content :deep(ol) { padding-left: 20px; margin: 6px 0 12px; }
.md-content :deep(pre) { max-width: 100%; overflow-x: auto; }
.md-content :deep(table) { display: block; max-width: 100%; overflow-x: auto; }
/* 来源引用徽标(与 ChatPanel 同款 chip;renderMarkdown 注入的 .md-ref
   是纯 DOM,scoped 组件里必须自带一份样式,否则徽标裸奔成普通文本) */
.md-content :deep(.md-ref) {
  display: inline-block;
  padding: 0 7px;
  margin: 0 2px;
  border: 1px solid currentColor;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.7;
  vertical-align: baseline;
  white-space: nowrap;
}
.md-content :deep(.md-ref-chunk) { background: #f9f0ff; color: #722ed1; }
.md-content :deep(.md-ref-entity) { background: var(--color-primary-bg); color: var(--color-primary-ink); }
.md-content :deep(.md-ref-triple) { background: #e6fffb; color: #08979c; }
.md-content :deep(.md-ref-rel) { background: #fff7e6; color: #d46b08; }
</style>
