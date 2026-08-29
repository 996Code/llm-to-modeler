<template>
  <div>
    <div class="ov-toolbar">
      <span class="ov-title">数据概览</span>
      <span v-if="stats?.firstAt" class="ov-range">数据范围 {{ fmtTime(stats.firstAt) }} ~ {{ fmtTime(stats.lastAt) }}</span>
      <a-button size="small" :loading="loading" @click="load">刷新</a-button>
    </div>

    <div class="ov-grid">
      <div class="ov-card">
        <div class="ov-icon" style="background: #eff6ff; color: #2563eb"><MessageOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.conversations ?? 0 }}</div>
          <div class="ov-label">会话总数</div>
          <div class="ov-extra">{{ stats?.users ?? 0 }} 个用户</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: #f0fdf4; color: #16a34a"><CommentOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.messages ?? 0 }}</div>
          <div class="ov-label">消息数</div>
          <div class="ov-extra">{{ stats?.events ?? 0 }} 条事件</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: #f5f3ff; color: #7c3aed"><RobotOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.llm ?? 0 }}</div>
          <div class="ov-label">LLM 调用</div>
          <div class="ov-extra">平均 {{ stats?.calls.avgDurationMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: #ecfeff; color: #0891b2"><CloudServerOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.upstream ?? 0 }}</div>
          <div class="ov-label">上游调用</div>
          <div class="ov-extra">合计 {{ stats?.calls.upstreamMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: #fff7ed; color: #ea580c"><AppstoreOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.packs?.enabled ?? 0 }}<span class="ov-dim">/​{{ stats?.packs?.discovered ?? 0 }}</span></div>
          <div class="ov-label">启用插件</div>
          <div class="ov-extra">热切换即时生效</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: #fdf2f8; color: #db2777"><NodeIndexOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.traceEvents ?? 0 }}</div>
          <div class="ov-label">链路打点</div>
          <div class="ov-extra">引擎自动 + 插件上报</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 概览:图标化统计卡片,数据结构与 backend get_admin_stats 对齐。
import { inject, onMounted, ref } from 'vue'
import {
  MessageOutlined, CommentOutlined, RobotOutlined, CloudServerOutlined,
  AppstoreOutlined, NodeIndexOutlined,
} from '@ant-design/icons-vue'
import { AdminStats, fetchStats, fmtTime } from '../api'
import type { LoadSafely } from './loadSafely'

const stats = ref<AdminStats | null>(null)
const loading = ref(false)
const loadSafely = inject<LoadSafely>('loadSafely')!

async function load() {
  loading.value = true
  await loadSafely(async () => {
    stats.value = await fetchStats()
  })
  loading.value = false
}

onMounted(load)
</script>

<style scoped>
.ov-toolbar { display: flex; align-items: center; gap: 14px; margin: 14px 0 18px; }
.ov-title { font-size: 16px; font-weight: 600; color: #1f2937; }
.ov-range { flex: 1; color: #9ca3af; font-size: 12px; }
.ov-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
}
.ov-card {
  display: flex; gap: 14px; align-items: center;
  border: 1px solid #eef0f4; border-radius: 12px; padding: 18px 16px;
  background: linear-gradient(180deg, #ffffff, #fbfcfe);
  transition: box-shadow 0.15s;
}
.ov-card:hover { box-shadow: 0 6px 18px rgba(30, 41, 82, 0.08); }
.ov-icon {
  width: 44px; height: 44px; border-radius: 10px; font-size: 20px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.ov-value { font-size: 24px; font-weight: 700; color: #111827; line-height: 1.1; }
.ov-dim { font-size: 14px; color: #9ca3af; font-weight: 500; }
.ov-label { font-size: 13px; color: #4b5563; margin-top: 3px; }
.ov-extra { font-size: 11px; color: #9ca3af; margin-top: 2px; }
</style>
