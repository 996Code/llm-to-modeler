<!--
  =============================================================================
  组件职责：嵌入模式（iframe）下的整体布局
  -----------------------------------------------------------------------------
  设计模式：精简组合 —— 相比独立布局，去掉侧边栏与 JSON 区，只保留顶部标题栏 +
            聊天面板。因为嵌入到宿主页面里，空间有限，且历史/JSON 由宿主管。
  Java 类比：相当于一个嵌入式小部件的视图控制器。

  仅在「嵌入模式」（store.isEmbedded === true）下由 App.vue 渲染。
  本组件被 embed.ts 创建的 iframe 加载（URL 带 embed=true 参数），
  与宿主页面之间通过嵌入契约 postMessage 双向通信（详见 composables/hostPort.ts）。
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
        <!-- 新对话：清空当前对话历史回到欢迎页（嵌入布局无侧栏，这是唯一的
             重开入口；后端会新建会话，AI 视角完全从零开始） -->
        <a-button
          type="text"
          size="small"
          class="new-chat-btn"
          :disabled="store.streaming"
          title="新对话（清空当前对话）"
          @click="startNewChat"
        >
          <PlusOutlined />
        </a-button>
        <!-- type="text" 无边框文字按钮；点击关闭通过 postMessage 通知父窗口 -->
        <a-button type="text" size="small" class="close-btn" @click="closeWindow">
          ✕
        </a-button>
      </div>
    </div>
    <!-- 链路错误横幅：握手/上下文失败时置顶展示（刷新页面可重试握手） -->
    <div v-if="store.hostLinkError" class="host-link-error">
      ⚠️ {{ store.hostLinkError }}
    </div>
    <!-- 聊天面板，传入 embedded 属性告诉它"我在嵌入模式"（会显示应用按钮）。
         版本回退能力在对话流的配置卡片上 -->
    <ChatPanel :embedded="true" />
  </div>
</template>

<script setup lang="ts">
// =============================================================================
// 脚本区：本布局的初始化逻辑（类比 Java Controller 的初始化方法）
// =============================================================================

// 图标组件（表单图标 / 新对话加号）
import { FormOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { message as antdMessage } from 'ant-design-vue'
// HostPort 单例：关闭走新协议
import { getHostPort } from '../composables/hostPort'
// 会话 Store（hostLinkError 驱动链路错误横幅）
import { useConversationStore } from '../stores/conversation'
// 子组件：聊天主面板（带 :embedded="true" 会让它显示「应用」按钮）
import ChatPanel from '../components/chat/ChatPanel.vue'

// HostPort 单例（嵌入态 = PostMessageHostPort）
const port = getHostPort()
// 会话 Store（取当前会话 ID 作为快照链的 key；hostLinkError 驱动链路错误横幅）
const store = useConversationStore()

// 隐藏窗口：发 CLOSE 让宿主收起悬浮窗（宿主只隐藏 iframe、会话保活）。
// 注意用 notifyClose 而非 close：close 会拆掉监听器，导致重开后
// APPLY_RESULT 收不到、应用永远超时——隐藏场景必须保活端口。
const closeWindow = () => port.notifyClose()

/**
 * 新对话：清空当前对话历史回到欢迎页（嵌入布局无侧栏，这是唯一的重开入口）。
 * 走 store.startNewConversation（后端新建会话 + 本地清 messages/currentConfig/
 * baseline），下一条消息从零开始（GET_CONTEXT 拉当前画布作基线）。
 */
async function startNewChat() {
  if (store.streaming) return
  try {
    await store.startNewConversation()
    antdMessage.success('已开始新对话')
  } catch {
    antdMessage.error('新建对话失败，请稍后重试')
  }
}
// 注：会话创建/恢复由 App.vue 的握手流程统一驱动（init → resume 或新建），
// 本布局不再 onMounted 抢跑创建，避免产生无 contextKey 绑定的孤儿会话。
// 版本回退能力在对话流里（配置卡片「回滚到此版本」），本布局不再自建版本 UI。
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

/* 链路错误横幅：红底置顶，明确告知嵌入链路异常（而非让 AI 看起来"笨"） */
.host-link-error {
  flex-shrink: 0;
  padding: 8px 14px;
  background: #fff1f0;
  border-bottom: 1px solid #ffa39e;
  color: #cf1322;
  font-size: 12px;
  line-height: 1.6;
}

.new-chat-btn { color: var(--text-secondary); }
.new-chat-btn:hover { color: var(--color-primary); background: var(--bg-hover) !important; }

.close-btn { color: var(--text-secondary); }
.close-btn:hover { color: var(--text-primary); background: var(--bg-hover) !important; }

/* ── 版本历史弹窗 ── */
</style>
