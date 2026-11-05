<template>
  <div class="ws-page">
    <div class="ws-page-head">
      <div class="ws-page-title">联网搜索</div>
      <div class="ws-page-sub">经 SearXNG(google,代理出网)检索外部信息;知识库与客服对话的联网能力共用本底座</div>
    </div>
    <WebSearchTester />
  </div>
</template>

<script setup lang="ts">
import WebSearchTester from './WebSearchTester.vue'
</script>

<style scoped>
.ws-page { padding: 4px 2px; }
.ws-page-head { margin-bottom: 16px; }
.ws-page-title { font-size: 16px; font-weight: 600; color: #1f2937; }
.ws-page-sub { font-size: 12px; color: #94a3b8; margin-top: 4px; }
</style>
