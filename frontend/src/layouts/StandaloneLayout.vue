<!--
  =============================================================================
  组件职责：独立运行模式下的整体布局
  -----------------------------------------------------------------------------
  设计模式：组合模式 —— 把页面拆成左侧会话列表 + 中间聊天区 + 右侧 JSON 区三栏。
  Java 类比：相当于一个主页面 Controller，编排多个子组件（子 Controller）协同工作。

  仅在「非嵌入模式」（store.isEmbedded === false）下由 App.vue 渲染。
  布局采用 Ant Design Vue 的 a-layout（含 a-layout-sider 侧边栏）。
  =============================================================================
-->
<template>
  <!-- a-layout：Ant Design 的布局容器，类似 Java Swing 的 BorderLayout 容器 -->
  <a-layout class="standalone-layout">
    <!-- 左侧侧边栏：品牌区 + 新建按钮 + 历史会话列表 -->
    <a-layout-sider :width="248" theme="light" class="sider">
      <!-- 顶部品牌 + 新建按钮 -->
      <div class="sider-header">
        <div class="brand">
          <div class="brand-logo">
            <!-- FormOutlined 是一个图标组件（来自 @ant-design/icons-vue） -->
            <FormOutlined />
          </div>
          <span class="brand-name">智能助手</span>
        </div>
        <!-- @click 绑定点击事件，调用 store 的动作（无需传参，直接引用方法） -->
        <a-button type="primary" class="new-btn" @click="store.startNewConversation">
          <!-- #icon 是具名插槽（slot），往按钮里塞图标 -->
          <template #icon><PlusOutlined /></template>
          新建对话
        </a-button>
      </div>
      <!-- 历史会话列表 -->
      <div class="conv-list">
        <div class="conv-list-title">历史对话</div>
        <!--
          v-for：循环渲染指令，类比 JSP 的 <c:forEach items="..." var="...">
          :key 是每项的唯一标识（类似数据库主键），Vue 用它做 diff 优化
          :class 是动态 class 绑定（对象语法：键为类名，值为布尔）
          @click 点击会话 → 选中并加载详情
        -->
        <div
          v-for="conv in store.conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: conv.id === store.currentConversation?.id }"
          @click="store.selectConversation(conv.id)"
        >
          <MessageOutlined class="conv-icon" />
          <span class="conv-title">{{ conv.title }}</span>
          <!-- @click.stop 阻止事件冒泡（点击删除时不触发外层会话选中） -->
          <DeleteOutlined class="conv-del" @click.stop="store.removeConversation(conv.id)" />
        </div>
        <!-- v-if 条件渲染：列表为空时显示占位文案 -->
        <div v-if="!store.conversations.length" class="empty-list">
          暂无历史对话
        </div>
      </div>
    </a-layout-sider>

    <!-- 中间聊天主区域 -->
    <div class="main-area">
      <ChatPanel />
    </div>

    <!-- 右侧 JSON 配置区 -->
    <div class="json-area">
      <JsonPanel />
    </div>
  </a-layout>
</template>

<script setup lang="ts">
// 从图标库导入用到的图标组件（每个图标是一个 Vue 组件）
import { PlusOutlined, MessageOutlined, DeleteOutlined, FormOutlined } from '@ant-design/icons-vue'
// 导入全局 store（单例，与 App.vue 中拿到的是同一份）
import { useConversationStore } from '../stores/conversation'
// 导入两个子组件（.vue 后缀可省略）
import ChatPanel from '../components/chat/ChatPanel.vue'
import JsonPanel from '../components/json/JsonPanel.vue'

// 实例化 store，模板里即可通过 store.xxx 访问状态/动作
const store = useConversationStore()
</script>

<!-- scoped：样式只作用于当前组件，不会泄漏到其它组件（Vue 自动加唯一属性选择器实现隔离） -->
<style scoped>
/* 高度撑满视口；横向排列三栏 */
.standalone-layout { height: 100vh; flex-direction: row; }

/* ===== 侧边栏 ===== */
.sider {
  border-right: 1px solid var(--border-color-light);
  display: flex;
  flex-direction: column;
  /* !important 覆盖 Ant Design 默认深色侧边栏背景为白 */
  background: var(--bg-container) !important;
}
.sider-header {
  padding: 16px;
  border-bottom: 1px solid var(--border-color-lighter);
}
.brand { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.brand-logo {
  width: 30px; height: 30px;
  border-radius: var(--radius-md);
  /* 渐变背景 */
  background: linear-gradient(135deg, var(--color-primary), #5b8cff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 15px;
  box-shadow: 0 2px 8px rgba(51, 112, 255, 0.3);
}
.brand-name { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.new-btn { width: 100%; border-radius: var(--radius-md) !important; height: 36px; }

/* 会话列表区域：flex:1 占满剩余高度，内容溢出可滚动 */
.conv-list { flex: 1; overflow-y: auto; padding: 8px; }
.conv-list-title {
  font-size: 12px;
  color: var(--text-placeholder);
  padding: 8px 8px 6px;
  font-weight: 500;
}
/* 单个会话项 */
.conv-item {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 10px;
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-regular);
  margin-bottom: 2px;
  /* 过渡动画（hover 时背景色平滑变化） */
  transition: background 0.2s, color 0.2s;
}
.conv-item:hover { background: var(--bg-hover); }
/* 选中态（active 类由 :class 动态绑定） */
.conv-item.active { background: var(--bg-active); color: var(--color-primary); font-weight: 500; }
.conv-icon { font-size: 14px; flex-shrink: 0; opacity: 0.7; }
/* 单行省略（超长标题用 ... 表示） */
.conv-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-del {
  font-size: 12px;
  /* 默认隐藏，hover 会话项时才显示 */
  opacity: 0;
  transition: opacity 0.2s, color 0.2s;
}
.conv-item:hover .conv-del { opacity: 0.5; }
.conv-del:hover { opacity: 1 !important; color: var(--color-danger); }
.empty-list {
  padding: 20px 10px;
  text-align: center;
  color: var(--text-placeholder);
  font-size: 13px;
}

/* ===== 主区域 ===== */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-page);
}
.json-area {
  width: 40%;
  max-width: 560px;
  border-left: 1px solid var(--border-color-light);
  display: flex;
  flex-direction: column;
  background: var(--bg-container);
}

/* 响应式：窄屏（≤1024px）改为纵向排列，隐藏侧边栏 */
@media (max-width: 1024px) {
  .standalone-layout { flex-direction: column; }
  .json-area { width: 100%; max-width: none; border-left: none; border-top: 1px solid var(--border-color-light); height: 40%; }
  .sider { display: none; }
}
</style>
