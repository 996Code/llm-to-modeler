<!--
  =============================================================================
  组件职责：JSON 配置展示面板（变更视图 / 复制 / 下载 / 应用到主系统）
  -----------------------------------------------------------------------------
  设计模式：纯展示组件（读取 store，不自持数据）。
    - 从 store 读取 currentConfig（AI 最新产出）与 baselineConfig（diff 基线）；
    - 变更渲染委托给 JsonDiffView（GitLab 风格红删绿增，与查看弹窗共用）；
    - 动作（复制/下载）调用浏览器原生 API；应用走事件委托。

  Java 类比：相当于一个只读详情视图（DTO 的 JSON 渲染器）。
  =============================================================================
-->
<template>
  <div class="json-panel">
    <!-- 顶部工具栏：标题 + 字段数标签 + 操作按钮组 -->
    <div class="panel-header">
      <div class="header-left">
        <CodeOutlined class="header-icon" />
        <span class="title">配置 JSON</span>
        <!-- v-if 有配置时才显示字段数标签 -->
        <a-tag v-if="config" color="processing" class="field-count">
          {{ store.currentConfigFieldCount || 0 }} 字段
        </a-tag>
      </div>
      <div class="actions">
        <!-- 复制按钮：无配置时禁用 -->
        <button class="icon-btn" @click="copy" :disabled="!config" title="复制">
          <CopyOutlined />
        </button>
        <!-- 下载按钮 -->
        <button class="icon-btn" @click="download" :disabled="!config" title="下载">
          <DownloadOutlined />
        </button>
        <!--
          应用到主系统按钮：仅嵌入模式显示（v-if）。
          实际应用逻辑在 ChatPanel.applyConfig（HostPort 信封协议），本组件只发事件。
        -->
        <button
          v-if="store.isEmbedded"
          class="icon-btn primary"
          @click="applyToParent"
          :disabled="!config"
          title="应用到主系统"
        >
          <CheckOutlined />
        </button>
      </div>
    </div>
    <!-- 内容区：空态 or 变更视图（含完整 JSON 兜底，见 JsonDiffView） -->
    <div class="editor-container">
      <!-- 空态：无配置时显示占位 -->
      <div v-if="!config" class="empty">
        <div class="empty-illustration">
          <FileTextOutlined />
        </div>
        <p class="empty-title">暂无配置</p>
        <p class="empty-desc">生成的配置将显示在这里</p>
      </div>
      <!--
        变更视图：基线（画布/上一版）vs 最新产出。
        有差异默认红删绿增（可切完整 JSON），无差异直接显示完整 JSON。
      -->
      <JsonDiffView v-else :oldObj="store.baselineConfig" :newObj="config" />
    </div>
  </div>
</template>

<script setup lang="ts">
// computed：派生响应式值（懒计算 + 缓存）
import { computed } from 'vue'
// message：Ant Design 的全局消息提示（类似 Java 的 toast/notify）
import { message } from 'ant-design-vue'
import { CopyOutlined, DownloadOutlined, FileTextOutlined, CodeOutlined, CheckOutlined } from '@ant-design/icons-vue'
import { useConversationStore } from '../../stores/conversation'
import { copyText } from '../../utils/clipboard'
import JsonDiffView from './JsonDiffView.vue'

// defineEmits：声明本组件会抛出的事件（模板事件 + 类型检查双保险）
const emit = defineEmits<{ (e: 'apply'): void }>()

const store = useConversationStore()

// 当前配置（从 store 派生，store.currentConfig 变化时自动更新）。
// 【类比 Java】computed 类似带缓存的 getter：依赖不变则不重算。
const config = computed(() => store.currentConfig)

/**
 * 复制配置 JSON 到剪贴板。
 * copyText 内部处理安全上下文：navigator.clipboard 仅 https/localhost
 * 可用，内网 http 访问走 execCommand 兜底。
 */
async function copy() {
  if (!config.value) return
  const ok = await copyText(JSON.stringify(config.value, null, 2))
  if (ok) message.success('已复制到剪贴板')
  else message.error('复制失败')
}

/**
 * 下载配置为本地 .json 文件。
 * 用 Blob + 临时 <a> 标签触发浏览器下载。
 */
function download() {
  if (!config.value) return
  // Blob：二进制大对象，指定 MIME 类型为 application/json
  const blob = new Blob([JSON.stringify(config.value, null, 2)], { type: 'application/json' })
  // 创建临时下载 URL
  const url = URL.createObjectURL(blob)
  // 临时 <a> 标签模拟点击下载
  const a = document.createElement('a')
  a.href = url
  // 文件名：表单名-时间戳.json（|| 兜底为 config）
  a.download = `${store.currentConfigName || 'config'}-${Date.now()}.json`
  a.click()
  // 释放临时 URL，避免内存泄漏
  URL.revokeObjectURL(url)
}

/**
 * 应用事件：委托给外层（ChatPanel.applyConfig 走 HostPort 信封协议）。
 */
function applyToParent() {
  if (!config.value) return
  emit('apply')
}
</script>

<style scoped>
.json-panel { display: flex; flex-direction: column; height: 100%; background: var(--bg-container); }

/* 顶部工具栏 */
.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color-light);
}
.header-left { display: flex; align-items: center; gap: 8px; }
.header-icon { color: var(--color-primary); font-size: 15px; }
.title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.field-count {
  margin-left: 4px !important;
  border: none !important;
  background: var(--color-primary-light) !important;
  color: var(--color-primary) !important;
  font-size: 11px;
}
.actions { display: flex; gap: 4px; }
/* 图标按钮通用样式 */
.icon-btn {
  width: 30px; height: 30px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color-light);
  background: var(--bg-container);
  color: var(--text-regular);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  transition: all 0.2s;
}
.icon-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-bg);
}
/* 主色按钮（应用到主系统） */
.icon-btn.primary { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.icon-btn.primary:hover:not(:disabled) { background: var(--color-primary-hover); border-color: var(--color-primary-hover); }
.icon-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 内容区 */
.editor-container { flex: 1; overflow: auto; min-height: 0; }
/* 空态 */
.empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.empty-illustration {
  width: 64px; height: 64px;
  border-radius: 50%;
  background: var(--bg-hover);
  color: var(--text-placeholder);
  display: flex; align-items: center; justify-content: center;
  font-size: 28px;
  margin-bottom: 16px;
}
.empty-title { font-size: 14px; color: var(--text-secondary); margin-bottom: 4px; }
.empty-desc { font-size: 12px; color: var(--text-placeholder); }
</style>
