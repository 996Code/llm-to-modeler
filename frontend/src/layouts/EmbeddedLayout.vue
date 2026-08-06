<!--
  =============================================================================
  组件职责：嵌入模式（iframe）下的整体布局
  -----------------------------------------------------------------------------
  设计模式：精简组合 —— 相比独立布局，去掉侧边栏与 JSON 区，只保留顶部标题栏 +
            聊天面板。因为嵌入到宿主页面里，空间有限，且历史/JSON 由宿主管。
  Java 类比：相当于一个嵌入式小部件的视图控制器。

  仅在「嵌入模式」（store.isEmbedded === true）下由 App.vue 渲染。
  本组件被 embed.ts 创建的 iframe 加载（URL 带 embed=true 参数），
  与宿主页面之间通过 postMessage 双向通信（详见 useEmbedBridge / embed.ts）。
  =============================================================================
-->
<template>
  <!-- 最外层容器：纵向 flex，高度撑满整个 iframe 视口 -->
  <div class="embedded-layout">
    <!-- 顶部标题栏：品牌 + 副标题 + 关闭按钮 -->
    <div class="embedded-header">
      <div class="header-brand">
        <div class="brand-logo">
          <FormOutlined />
        </div>
        <div class="header-text">
          <span class="title">智能助手</span>
          <span class="subtitle">自然语言驱动，多场景智能服务</span>
        </div>
      </div>
      <div class="header-actions">
        <!-- type="text" 无边框文字按钮；点击关闭通过 postMessage 通知父窗口 -->
        <a-button type="text" size="small" class="close-btn" @click="closeWindow">
          <CloseOutlined />
        </a-button>
      </div>
    </div>
    <!-- 聊天面板，传入 embedded 属性告诉它"我在嵌入模式"（会显示应用按钮） -->
    <ChatPanel :embedded="true" />
  </div>
</template>

<script setup lang="ts">
// =============================================================================
// 脚本区：本布局的初始化逻辑（类比 Java Controller 的初始化方法）
// =============================================================================

// onMounted：组件挂载完成钩子。
// 【类比 Java】相当于 @PostConstruct —— 在组件实例创建并挂载到 DOM 后执行一次。
import { onMounted } from 'vue'
// 图标组件（关闭 X、表单图标）
import { CloseOutlined, FormOutlined } from '@ant-design/icons-vue'
// 全局会话 Store（Pinia 单例），类比 @Autowired private ConversationStore store
import { useConversationStore } from '../stores/conversation'
// useEmbedBridge：嵌入通信桥组合式函数。
// 【职责】封装「iframe 内层 → 宿主父窗口」的 postMessage 通信，
//        提供 closeWindow 等便捷方法，避免在各处手写 window.parent.postMessage。
import { useEmbedBridge } from '../composables/useEmbedBridge'
// 子组件：聊天主面板（带 :embedded="true" 会让它显示「应用配置」按钮）
import ChatPanel from '../components/chat/ChatPanel.vue'

// 获取全局 store 实例（setup 中调用一次）
const store = useConversationStore()
// 从桥中解构出 closeWindow（其它方法这里用不到，故不解构，类比 Java 的方法引用）
const { closeWindow } = useEmbedBridge()

// 组件挂载后立即创建新会话。
// 【设计意图】嵌入模式下不依赖历史会话（历史由宿主系统管理），
//            每次刷新/重新打开都开一个全新对话，保证宿主每次拿到的都是干净上下文。
onMounted(async () => {
  // 嵌入模式：每次刷新都创建新会话
  // （嵌入态下不依赖历史，每次打开都是全新对话）
  await store.startNewConversation()
})
</script>

<style scoped>
/* 嵌入布局：纵向排列标题栏 + 聊天区，高度撑满 iframe */
.embedded-layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-page);
}
.embedded-header {
  display: flex;
  justify-content: space-between;  /* 两端对齐：品牌靠左，按钮靠右 */
  align-items: center;
  padding: 12px 16px;
  background: var(--bg-container);
  border-bottom: 1px solid var(--border-color-light);
  box-shadow: var(--shadow-sm);
}
.header-brand { display: flex; align-items: center; gap: 10px; }
.brand-logo {
  width: 32px; height: 32px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--color-primary), #5b8cff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  box-shadow: 0 2px 8px rgba(51, 112, 255, 0.3);
}
.header-text { display: flex; flex-direction: column; line-height: 1.3; }
.title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.subtitle { font-size: 11px; color: var(--text-secondary); }
.header-actions { display: flex; gap: 4px; }
.close-btn { color: var(--text-secondary); }
.close-btn:hover { color: var(--text-primary); background: var(--bg-hover) !important; }
</style>
