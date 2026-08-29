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
    <!-- 顶部标题栏：品牌 + 历史对话入口 + 关闭按钮 -->
    <div class="embedded-header">
      <div class="header-brand">
        <div class="brand-logo">
          <FormOutlined />
        </div>
        <div class="header-text">
          <span class="title">智能助手</span>
          <span class="subtitle">自然语言驱动，多场景智能服务</span>
        </div>
        <!-- 历史对话：嵌入布局无侧栏，这是用户翻自己历史会话的唯一入口。
             列表按当前用户隔离（X-User-Id=宿主下发 userId），点选后
             覆盖当前对话继续沟通（有确认提示，防误触丢上下文） -->
        <a-button type="text" size="small" class="history-btn" title="历史对话"
          @click="openHistory">
          <HistoryOutlined />
          <span class="history-label">历史对话</span>
        </a-button>
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

    <!-- 历史对话抽屉：仅当前用户的会话；点选 → 覆盖确认 → 载入继续沟通 -->
    <a-drawer v-model:open="historyOpen" placement="left" width="300" title="历史对话"
      class="history-drawer" :body-style="{ padding: '8px' }">
      <a-spin v-if="historyLoading" style="display: block; padding: 40px 0; text-align: center" />
      <a-empty v-else-if="!historyList.length" description="暂无历史对话" style="padding: 40px 0" />
      <div v-else class="history-list">
        <div
          v-for="conv in historyList"
          :key="conv.id"
          class="history-item"
          :class="{ active: conv.id === store.currentConversation?.id }"
          @click="pickConversation(conv)"
        >
          <div class="item-title">{{ conv.displayTitle || conv.title }}</div>
          <div class="item-meta">
            <span>{{ conv.messageCount ?? 0 }} 条消息</span>
            <span>{{ relativeTime(conv.updatedAt) }}</span>
          </div>
        </div>
      </div>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// =============================================================================
// 脚本区：本布局的初始化逻辑（类比 Java Controller 的初始化方法）
// =============================================================================

// 图标组件（表单图标 / 新对话加号 / 历史对话）
import { FormOutlined, PlusOutlined, HistoryOutlined } from '@ant-design/icons-vue'
import { message as antdMessage, Modal } from 'ant-design-vue'
import { ref } from 'vue'
// HostPort 单例：关闭走新协议
import { getHostPort } from '../composables/hostPort'
// 会话 Store（hostLinkError 驱动链路错误横幅）
import { useConversationStore } from '../stores/conversation'
// API：历史会话列表（嵌入态 X-User-Id=宿主下发 userId，天然按用户隔离）
import { listConversations } from '../services/api'
import type { Conversation } from '../types'
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

// ── 历史对话：列表 + 覆盖载入 ─────────────────────────────
const historyOpen = ref(false)
const historyLoading = ref(false)
const historyList = ref<Conversation[]>([])

/** 打开抽屉并刷新列表（每次打开都拉最新，覆盖后回来看状态也是新的） */
async function openHistory() {
  historyOpen.value = true
  historyLoading.value = true
  try {
    historyList.value = await listConversations()
  } catch {
    antdMessage.error('历史对话加载失败')
    historyList.value = []
  } finally {
    historyLoading.value = false
  }
}

/**
 * 点选历史会话：先提示覆盖（当前对话内容将被替换，防误触丢上下文），
 * 确认后载入（消息 + 最新配置 + 快照链），之后的消息都发往该会话继续沟通。
 */
function pickConversation(conv: Conversation) {
  if (store.streaming) {
    antdMessage.warning('正在回复中，请稍后再切换')
    return
  }
  if (conv.id === store.currentConversation?.id) {
    historyOpen.value = false  // 点的就是当前会话：直接收起
    return
  }
  const title = conv.displayTitle || conv.title
  Modal.confirm({
    title: '覆盖当前对话？',
    content: `将载入「${title}」的对话内容与最新配置，当前未应用的变更将被替换。`,
    okText: '覆盖载入',
    cancelText: '取消',
    onOk: async () => {
      try {
        await store.selectConversation(conv.id)
        historyOpen.value = false
        antdMessage.success(`已载入「${title}」，可继续沟通`)
      } catch {
        antdMessage.error('载入失败，请稍后重试')
      }
    },
  })
}

/** 相对时间（列表用：「5 分钟前 / 今天 14:20 / 08-21」） */
function relativeTime(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diff = Date.now() - d.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const pad = (n: number) => String(n).padStart(2, '0')
  return sameDay
    ? `今天 ${pad(d.getHours())}:${pad(d.getMinutes())}`
    : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
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

/* 历史对话入口（左上角，品牌区右侧） */
.history-btn { margin-left: 10px; color: var(--text-secondary); }
.history-btn :deep(span) { display: inline-flex; align-items: center; gap: 4px; }
.history-label { font-size: 12px; }
.history-list { display: flex; flex-direction: column; gap: 4px; }
.history-item {
  padding: 8px 10px; border-radius: var(--radius-md, 8px); cursor: pointer;
  border: 1px solid transparent;
}
.history-item:hover { background: var(--bg-hover, #f5f7fa); }
.history-item.active {
  background: var(--color-primary-bg, #f0f5ff);
  border-color: var(--color-primary, #3370ff);
}
.item-title {
  font-size: 13px; color: var(--text-primary); font-weight: 500;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.item-meta {
  display: flex; justify-content: space-between; margin-top: 3px;
  font-size: 11px; color: var(--text-secondary);
}

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
