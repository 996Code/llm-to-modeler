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
</style>
