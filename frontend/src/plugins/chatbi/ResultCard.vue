<template>
  <div v-if="view" class="chatbi-result">
    <div v-if="view.answer" class="result-answer md-content" v-html="renderMarkdown(view.answer)" />
    <BiChartCard
      ref="chartRef"
      :chart="view.chart"
      :artifact="artifact"
      :metric-hits="view.detail.metricHits"
      :detail="view.detail"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import BiChartCard from '../../components/chat/BiChartCard.vue'
import { biPresentation } from './presentation'

const props = defineProps<{
  artifact: Record<string, any>
  summary: string
  labels: Record<string, string>
  renderMarkdown: (text: string) => string
  compact: boolean
  citedChunks: number[]
  detail?: Record<string, any> | null
}>()
const chartRef = ref<InstanceType<typeof BiChartCard> | null>(null)
const view = computed(() => biPresentation({ role: 'assistant', dataResult: props.artifact, content: props.summary }, props.detail))
defineExpose({ getChartInstance: () => chartRef.value?.getChartInstance?.() ?? null })
</script>

<style scoped>
.chatbi-result { width: 100%; min-width: 0; }
.result-answer { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; margin-bottom: 12px; }
.result-answer :deep(p) { margin: 0 0 8px; }
.result-answer :deep(ul) { padding-left: 20px; margin: 6px 0 12px; }
</style>
