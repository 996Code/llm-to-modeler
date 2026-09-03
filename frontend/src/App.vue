<!--
  =============================================================================
  模块说明：应用根组件（App.vue，单文件组件 SFC）
  -----------------------------------------------------------------------------
  类比 Java：相当于整个 Web 应用的"总入口 Controller / Dispatcher"。
  一个 .vue 文件 = 一个组件 = 三部分：
    <template>  HTML 模板（视图层，类似 JSP / Thymeleaf 模板）
    <script>    TS/JS 逻辑（控制层，类似 Controller + Bean）
    <style>     CSS 样式（表现层）

  职责：
    1. 根据运行模式（独立 / 嵌入）选择不同的布局组件
    2. 在组件挂载后初始化：嵌入模式监听父系统消息，独立模式加载历史对话
    3. 定义全局 CSS 变量（设计令牌 design tokens）供所有子组件复用
  =============================================================================
-->
<template>
  <!--
    v-if / v-else：条件渲染指令。
    类比 Java/JSP 的 <c:if test="..."> / <c:choose>。
    store.isEmbedded 为 true 渲染 EmbeddedLayout，否则渲染 StandaloneLayout。
  -->
  <StandaloneLayout v-if="!store.isEmbedded" />
  <EmbeddedLayout v-else />
</template>

<!--
  <script setup lang="ts"> 是 Vue 3 的 Composition API 语法糖：
  - setup：表示这是组件的"构造/初始化"阶段（类比 @PostConstruct 之前的那段逻辑）
  - lang="ts"：使用 TypeScript
  - 顶层声明的变量会自动暴露给 <template> 使用（无需 return）
-->
<script setup lang="ts">
// 从 vue 导入生命周期钩子 onMounted。
// onMounted：组件挂载到 DOM 后触发，类比 Java 的 @PostConstruct。
import { onMounted } from 'vue'
// 导入 Pinia store（全局单例状态容器，类比 Spring @Service 单例 Bean）
import { useConversationStore } from './stores/conversation'
// HostPort 单例：嵌入态为 PostMessageHostPort，独立态为 NullHostPort
// （INIT 下发的鉴权头由 hostPort 内部写入 forwardHeaders，此处不再经手）
import { getHostPort } from './composables/hostPort'
// 宿主下发的 userId 存这里（供 api.ts 取 X-User-Id）
import { setUserId } from './composables/userIdentity'
// 导入两个布局组件（.vue 可省略后缀，构建工具自动解析）
import StandaloneLayout from './layouts/StandaloneLayout.vue'
import EmbeddedLayout from './layouts/EmbeddedLayout.vue'

// 实例化 store。同一个 store 在整个应用中是单例，多次调用拿到的是同一份状态。
const store = useConversationStore()

// 组件挂载完成后的初始化逻辑（@PostConstruct）
// 嵌入模式：握手成功只做身份/绑定登记，首开即新对话——不自动恢复
// 上一会话（继续旧对话走右上角历史抽屉显式载入）。
onMounted(() => {
  if (store.isEmbedded) {
    const port = getHostPort()
    port.init().then((r) => {
      if (!r) {
        // 3s 无 INIT（宿主未实现契约/链路断）：显式报错而非静默降级——
        // 横幅告诉用户 AI 无法读写画布，避免"AI 笨"的误解
        store.hostLinkError = '宿主握手失败（3 秒内未收到 INIT）：AI 将无法读写画布，请刷新页面重试'
        void store.startNewConversation()
        return
      }
      // 宿主下发的 userId：写入 userIdentity，供所有 API 请求的 X-User-Id 使用
      if (r.userId) setUserId(r.userId)
      // contextKey 存入 localStorage，供懒创建会话时绑定（sendMessage 内读取）
      if (r.contextKey) localStorage.setItem('embedded_context_key', r.contextKey)
      // token 刷新推送：userId 同步（headers 已由 hostPort 内部写入 forwardHeaders）
      port.onAuthUpdated((id) => {
        if (id?.userId) setUserId(id.userId)
      })
      void store.startNewConversation()
    })
  } else {
    // 独立页面加载历史对话
    store.loadConversations()
  }
})
</script>

<!--
  <style>（非 scoped）：全局样式。全局 CSS 变量（design tokens）定义在 :root，
  所有组件都能通过 var(--color-primary) 等方式引用。
  类比 Java 的 application.yml 全局配置，或设计系统的基础变量。
-->
<style>
/* ===== 设计令牌（Design tokens，参考 MaxKB 风格）===== */
/* :root 选择器对应 <html> 根元素，定义的 CSS 变量全局可见 */
:root {
  /* 主题色 */
  --color-primary: #3370ff;
  --color-primary-hover: #2860e6;
  --color-primary-light: #eaf0ff;
  --color-primary-bg: #f0f4ff;
  --color-success: #00a870;
  --color-danger: #f54a45;
  --color-warning: #ff9e29;

  /* 文字色阶 */
  --text-primary: #1f2329;
  --text-regular: #4e5969;
  --text-secondary: #86909c;
  --text-placeholder: #c9cdd4;

  /* 边框色阶 */
  --border-color: #e5e6eb;
  --border-color-light: #f0f1f3;
  --border-color-lighter: #f7f8fa;

  /* 背景色 */
  --bg-page: #f5f6f8;
  --bg-container: #ffffff;
  --bg-hover: #f7f8fa;
  --bg-active: var(--color-primary-light);

  /* 圆角 */
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;

  /* 阴影 */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 2px 8px rgba(0, 0, 0, 0.06);
  --shadow-input: 0 2px 12px rgba(51, 112, 255, 0.08);

  /* 字体 */
  --font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  --font-mono: 'SF Mono', 'Fira Code', 'JetBrains Mono', Menlo, Consolas, monospace;
}

/* 全局重置：清除默认 margin/padding，统一 box-sizing 模型（border-box 更直观） */
* { margin: 0; padding: 0; box-sizing: border-box; }
/* html/body/#app 高度 100%，保证应用能撑满整个视口 */
html, body, #app { height: 100%; font-family: var(--font-family); color: var(--text-primary); }

/* 自定义滚动条样式（仅 WebKit 内核浏览器生效） */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #d5d7db; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #b8babe; }

/* 覆盖 Ant Design 主色，使其与 MaxKB 品牌色一致。
   !important 用于强制覆盖组件库默认样式（优先级最高） */
.ant-btn-primary {
  background: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}
.ant-btn-primary:not(:disabled):hover {
  background: var(--color-primary-hover) !important;
  border-color: var(--color-primary-hover) !important;
}
.ant-tag {
  border-radius: var(--radius-md) !important;
}
</style>
