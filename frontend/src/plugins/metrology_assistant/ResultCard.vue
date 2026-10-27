<template>
  <div v-if="view" class="metrology-result">
    <header class="result-heading"><strong>{{ view.title }}</strong><a-tag :color="view.kind === 'notice' ? 'warning' : 'processing'">{{ view.kind === 'notice' ? '业务提示' : view.status || '已完成' }}</a-tag></header>
    <div v-if="view.answer" class="md-content" v-html="renderMarkdown(view.answer)" />
    <dl v-if="view.fields" class="result-fields"><div v-for="field in view.fields" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div></dl>
    <section v-for="(record, i) in view.records || []" :key="i" class="result-record">
      <p v-if="(view.records?.length || 0) > 1" class="result-record-index">记录 {{ i + 1 }}</p>
      <dl class="result-fields"><div v-for="field in record" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div></dl>
    </section>
    <section v-if="view.kind === 'table'" class="result-table">
      <div class="table-scroll">
        <table>
          <thead><tr><th v-for="col in view.columns" :key="col">{{ col }}</th></tr></thead>
          <tbody>
            <tr v-for="(row, i) in view.rows" :key="i">
              <td v-for="(cell, j) in row" :key="j">{{ cell }}</td>
            </tr>
            <tr v-if="!view.rows?.length"><td :colspan="view.columns?.length || 1" class="empty">无数据行</td></tr>
          </tbody>
        </table>
      </div>
      <p v-if="(view.rowTotal || 0) > (view.rows?.length || 0)" class="table-more">
        共 {{ view.rowTotal }} 行,仅展示前 {{ view.rows?.length }} 行
      </p>
    </section>
    <KgGraphCard v-if="view.kind === 'knowledge'" :result="artifact" :cited-chunks="citedChunks" :compact="compact" />
  </div>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import KgGraphCard from '../../components/chat/KgGraphCard.vue'
import { metrologyPresentation } from './presentation'
const props = defineProps<{
  artifact: Record<string, any>; summary: string; labels: Record<string, string>
  renderMarkdown: (text: string) => string; compact: boolean; citedChunks: number[]
}>()
const view = computed(() => metrologyPresentation({ role: 'assistant', content: props.summary, dataResult: props.artifact }, props.labels))
</script>
<style scoped>
.result-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.result-heading strong { font-size: 14px; font-weight: 600; }
.result-heading :deep(.ant-tag) { margin: 0; flex-shrink: 0; }
.result-fields { margin: 0; font-size: 13px; }
.result-fields > div { display: grid; grid-template-columns: minmax(70px, 28%) minmax(0, 1fr); gap: 12px; padding: 9px 0; border-bottom: 1px solid #edf0f3; }
.result-fields > div:last-child { border-bottom: 0; }
.result-fields dt { color: #7a8491; }
.result-fields dd { margin: 0; color: #253046; overflow-wrap: anywhere; }
.result-record + .result-record { border-top: 1px dashed #dfe3e9; margin-top: 10px; padding-top: 10px; }
.result-record-index { margin: 0 0 4px; font-size: 12px; color: #8c939e; }
.result-table { margin-top: 4px; }
.table-scroll { overflow-x: auto; }
.result-table table { border-collapse: collapse; width: 100%; font-size: 13px; }
.result-table th, .result-table td { border: 1px solid #edf0f3; padding: 6px 10px; text-align: left; overflow-wrap: anywhere; }
.result-table th { background: #f7f9fb; color: #5a6572; font-weight: 500; white-space: nowrap; }
.result-table td { color: #253046; }
.result-table .empty { color: #8c939e; text-align: center; }
.table-more { margin: 6px 0 0; font-size: 12px; color: #8c939e; }
.md-content { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; margin-bottom: 12px; }
.md-content :deep(p) { margin: 0 0 8px; }
.md-content :deep(ul) { padding-left: 20px; margin: 6px 0 12px; }
</style>
