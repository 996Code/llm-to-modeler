<!--
  =============================================================================
  组件职责：JSON 配置展示面板（带语法高亮 / 复制 / 下载 / 应用到主系统）
  -----------------------------------------------------------------------------
  设计模式：纯展示组件（读取 store，不自持数据）。
    - 从 store 读取 currentConfig；
    - computed 派生格式化 JSON 文本 + 高亮 HTML；
    - 动作（复制/下载/应用）调用浏览器原生 API。
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
          通过 postMessage 把配置发回父窗口。
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
    <!-- 内容区：空态 or 高亮 JSON -->
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
        v-else 与上方 v-if 互斥。
        v-html 把字符串当 HTML 渲染（这里渲染高亮后的 JSON，含 <span> 标签）。
        ⚠ v-html 有 XSS 风险，这里数据来自本地配置且已转义，可安全使用。
      -->
      <pre v-else class="json-view" v-html="highlightedJson"></pre>
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

const store = useConversationStore()

// 当前配置（从 store 派生，store.currentConfig 变化时自动更新）。
// 【类比 Java】computed 类似带缓存的 getter：依赖不变则不重算。
const config = computed(() => store.currentConfig)

// 格式化为 2 空格缩进的 JSON 字符串；无配置则空串。
// JSON.stringify(obj, null, 2)：第三参 2 表示缩进空格数。
// 这里只做纯文本格式化，着色交给下面的 highlightedJson。
const formattedJson = computed(() =>
  config.value ? JSON.stringify(config.value, null, 2) : '',
)

// Minimal JSON syntax highlighter (escape → wrap keys/strings/numbers/bools)
// 自实现的轻量 JSON 语法高亮：
//   1. 先转义 < > & 防止 HTML 注入；
//   2. 用正则把 key/字符串/数字/布尔 包进 <span class="j-xxx"> 里；
//   3. CSS 按类名着色。
const highlightedJson = computed(() => {
  if (!formattedJson.value) return ''
  // 转义特殊字符，避免被当作 HTML 标签解析
  const escaped = formattedJson.value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  // 正则匹配四类 token，回调里分别包 span（key/str/num/bool）
  // 【正则分解】四个捕获组（用 | 并列，按顺序尝试匹配）：
  //   1. key   —— "..." 紧跟冒号，识别对象键
  //   2. str   —— "..." 字符串值
  //   3. num   —— 数字（含小数和负号）
  //   4. bool  —— true/false/null 字面量
  // /g 表示全局匹配（替换所有）。(?:\\.|[^"\\]) 匹配转义字符或非引号字符。
  return escaped.replace(
    /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|(\b-?\d+\.?\d*\b)|(\btrue\b|\bfalse\b|\bnull\b)/g,
    // replace 回调的参数：整体匹配 + 每个捕获组（只有命中的那个非 undefined）
    (match, key, str, num, bool) => {
      // key：去掉末尾冒号后包 span，再把冒号加回去（冒号不染色）
      if (key) return `<span class="j-key">${key.slice(0, -1).replace(/:$/, '')}</span>:`
      // 字符串值：整体包绿色 span
      if (str) return `<span class="j-str">${str}</span>`
      // 数字：黄色 span
      if (num) return `<span class="j-num">${num}</span>`
      // 布尔/ null：红色 span
      if (bool) return `<span class="j-bool">${bool}</span>`
      // 未匹配任何组，原样返回（安全兜底）
      return match
    },
  )
})

/**
 * 复制配置 JSON 到剪贴板。
 * navigator.clipboard 是浏览器原生剪贴板 API（需 https 或 localhost）。
 */
async function copy() {
  if (!config.value) return
  await navigator.clipboard.writeText(JSON.stringify(config.value, null, 2))
  // 弹出成功提示
  message.success('已复制到剪贴板')
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
 * 把当前配置通过 postMessage 发送给父窗口（嵌入模式下供宿主应用）。
 */
function applyToParent() {
  if (!config.value) return
  window.parent.postMessage(
    { type: 'MODELER_CONFIG_APPLY', payload: { config: config.value } },
    '*',
  )
  message.success('已发送到主系统')
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
.editor-container { flex: 1; overflow: auto; }
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

/* JSON 显示区：等宽字体、保留空白 */
.json-view {
  padding: 16px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-regular);
  white-space: pre-wrap;
  word-break: break-all;
}
/* :deep() 穿透 scoped，给 v-html 注入的 span 着色（scoped 默认改不到 v-html 内容） */
.json-view :deep(.j-key) { color: #1f6feb; }
.json-view :deep(.j-str) { color: #00a870; }
.json-view :deep(.j-num) { color: #d4a72c; }
.json-view :deep(.j-bool) { color: #f54a45; }
</style>
