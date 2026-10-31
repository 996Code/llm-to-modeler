<template>
  <!-- 链路/调用明细的格式化渲染(聊天窗链路弹窗/管理端链路追踪/调用日志抽屉共用) -->
  <div class="tds">
    <section v-for="(sec, i) in sections" :key="i" class="tds-sec">
      <div v-if="sec.title" class="tds-title">{{ sec.title }}</div>
      <div v-if="sec.rows" class="tds-rows">
        <div v-for="[k, v] in sec.rows" :key="k" class="tds-row">
          <span class="tds-k">{{ k }}</span><span class="tds-v">{{ v }}</span>
        </div>
      </div>
      <ul v-if="sec.list" class="tds-list">
        <li v-for="(item, j) in sec.list" :key="j">{{ item }}</li>
      </ul>
      <pre v-if="sec.text" class="tds-text">{{ sec.text }}</pre>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { DetailSection } from './traceDetail'
defineProps<{ sections: DetailSection[] }>()
</script>

<style scoped>
.tds { display: flex; flex-direction: column; gap: 8px; }
.tds-title {
  font-size: 11.5px; font-weight: 600; color: var(--text-secondary, #7d8592);
  margin-bottom: 3px;
}
.tds-row { display: flex; gap: 8px; font-size: 12px; line-height: 1.7; }
.tds-k { flex-shrink: 0; min-width: 5.5em; color: var(--text-secondary, #7d8592); }
.tds-v { color: var(--text-primary, #1f2329); word-break: break-all; min-width: 0; }
.tds-list { margin: 2px 0 0; padding-left: 16px; font-size: 12px; line-height: 1.8;
  color: var(--text-primary, #1f2329); }
.tds-text {
  margin: 2px 0 0; padding: 6px 8px; font-size: 11.5px; line-height: 1.6;
  font-family: var(--font-mono, monospace); white-space: pre-wrap; word-break: break-all;
  background: var(--bg-hover, #f5f6f8); border-radius: 6px;
  color: var(--text-regular, #3b4252); max-height: 180px; overflow-y: auto;
}
</style>
