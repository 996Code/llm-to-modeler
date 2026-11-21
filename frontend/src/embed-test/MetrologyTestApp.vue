<template>
  <div class="mt">
    <header class="mt-header">
      <div class="mt-heading"><CustomerServiceOutlined /><h1>智能客服测试</h1><a-tag>联调环境</a-tag></div>
    </header>
    <div class="mt-workspace">
      <section class="mt-conversation" aria-label="客服嵌入测试">
        <div class="mt-preview">
          <iframe v-if="frameOpen" title="计量智能客服测试对话" class="mt-frame" :src="frameSrc" />
          <div v-else class="mt-frame mt-frame-closed" role="status">
            <span>对话窗口已收起</span>
            <button type="button" class="mt-reopen" @click="reopenFrame">重新打开</button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { CustomerServiceOutlined } from '@ant-design/icons-vue'

const frameSrc = '/ai-modeler/?embed=true&hostless=1&packs=metrology_assistant'
const frameOpen = ref(true)
// 宿主侧 CLOSE 处理:iframe 内点 ✕ 发 postMessage(hostless 无 SDK 宿主,
// 测试页自己充当宿主)——收起预览并提供重新打开入口
function reopenFrame() {
  frameOpen.value = true
}
function onChildMessage(e: MessageEvent) {
  const d = e.data as { src?: string; type?: string } | null
  if (d && d.src === 'child' && d.type === 'CLOSE') frameOpen.value = false
}
onBeforeUnmount(() => window.removeEventListener('message', onChildMessage))
if (typeof window !== 'undefined') {
  window.addEventListener('message', onChildMessage)
}
</script>

<style scoped>
.mt { color: #1f2329; padding: 20px; }
.mt-header, .mt-heading { display: flex; align-items: center; }
.mt-header { justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.mt-heading { gap: 10px; min-width: 0; flex-wrap: wrap; }
.mt-heading > .anticon { color: var(--brand); font-size: 20px; }
.mt-heading h1 { font-size: 18px; font-weight: 600; margin: 0; }
.mt-heading :deep(.ant-tag) { margin: 0; color: #8c651a; background: #fff8e8; border-color: #f3e3bf; }
.mt-workspace { display: grid; grid-template-columns: minmax(0, 1fr); border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; height: max(520px, calc(100dvh - 210px)); }
.mt-conversation { min-width: 0; min-height: 0; }
.mt-preview { height: 100%; }
.mt-frame { width: 100%; height: 100%; border: 0; }
.mt-frame-closed { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; background: #fafbfc; color: #626b78; font-size: 13px; }
.mt-reopen { border: 1px solid #d0d3d9; background: #fff; border-radius: 6px; padding: 5px 14px; cursor: pointer; font-size: 12px; color: #1f2329; }
.mt-reopen:hover { border-color: var(--brand); color: var(--brand); }
</style>
