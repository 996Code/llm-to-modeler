<!--
  =============================================================================
  组件职责：聊天页右上角「服务状态」徽标 + hover 弹出的系统配置面板
  -----------------------------------------------------------------------------
  设计模式：组合模式 —— 状态探测（GET /api/health）与系统配置开关面板
  （sysConfig 单例）封装在一个自包含组件里，StandaloneLayout 与
  EmbeddedLayout 直接放置即可，两布局共享同一份配置状态。

  交互：鼠标悬浮徽标 → a-popover 弹出「系统配置」面板，内含两个开关：
    1. 显示链路与思考信息（showPipeline，默认开）
    2. 显示检索结果（showRetrieval，默认开）
  开关状态由 sysConfig composable 持久化到 localStorage，跨会话生效。

  样式参照管理端 AdminApp 的 shell-actions（a-badge + 间距）。
  =============================================================================
-->
<template>
  <!-- trigger="hover"：悬浮触发（需求原文：鼠标悬浮上去之后出现系统配置） -->
  <a-popover trigger="hover" placement="bottomRight" overlay-class-name="sys-config-popover">
    <template #title>系统配置</template>
    <template #content>
      <div class="sys-config-panel">
        <!-- 开关 1：对话时的链路/路径/思考信息 -->
        <div class="config-row">
          <div class="config-text">
            <div class="config-label">显示链路与思考信息</div>
            <div class="config-desc">关闭后仅显示「正在处理…」与最终结果</div>
          </div>
          <a-switch v-model:checked="sysConfig.showPipeline" size="small" />
        </div>
        <!-- 开关 2：首页对话界面的检索结果 -->
        <div class="config-row">
          <div class="config-text">
            <div class="config-label">显示检索结果</div>
            <div class="config-desc">关闭后对话中不显示知识图谱检索结果卡</div>
          </div>
          <a-switch v-model:checked="sysConfig.showRetrieval" size="small" />
        </div>
      </div>
    </template>
    <!-- 徽标本体：绿点=服务正常 / 红点=服务异常 / 探测中=蓝色脉动 -->
    <span class="service-status-badge">
      <a-badge :status="badgeStatus" :text="badgeText" />
    </span>
  </a-popover>
</template>

<script setup lang="ts">
// ref/computed：响应式状态；onMounted：挂载后探测（不阻塞首屏渲染）
import { ref, computed, onMounted } from 'vue'
// 健康探测：GET /api/health（鉴权白名单内，无需 token）
import { checkHealth } from '../services/api'
// 系统配置单例：两个显示开关（localStorage 持久化）
import { useSysConfig } from '../composables/sysConfig'

const sysConfig = useSysConfig()

// 服务健康状态：null=探测中（首帧），true/false=探测结果
const serviceOk = ref<boolean | null>(null)

// 挂载后探测一次：绿点「服务正常」/ 红点「服务异常」。
// 失败不重试不轮询——徽标只做一次性状态指示，避免常驻后台请求。
onMounted(async () => {
  serviceOk.value = await checkHealth()
})

// 徽标状态映射：探测中用 processing（蓝色脉动），结果用 success/error
const badgeStatus = computed(() =>
  serviceOk.value === null ? 'processing' : serviceOk.value ? 'success' : 'error',
)
const badgeText = computed(() =>
  serviceOk.value === null ? '检测中' : serviceOk.value ? '服务正常' : '服务异常',
)
</script>

<style scoped>
/* 徽标容器：给 popover 一个稳定的 hover 目标（内联块 + 内边距扩大热区） */
.service-status-badge {
  display: inline-flex;
  align-items: center;
  cursor: default;
  padding: 2px 4px;
  border-radius: var(--radius-md, 6px);
  transition: background 0.2s;
}
.service-status-badge:hover {
  background: var(--bg-hover, #f5f7fa);
}

/* ===== 系统配置面板（popover content） ===== */
.sys-config-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 240px;
}
.config-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 4px;
}
.config-row + .config-row {
  border-top: 1px solid var(--border-color-lighter, #f0f0f0);
}
.config-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.config-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary, #1f2329);
}
.config-desc {
  font-size: 12px;
  color: var(--text-secondary, #8f959e);
  line-height: 1.4;
}
</style>
