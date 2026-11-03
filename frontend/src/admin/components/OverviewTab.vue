<template>
  <div>
    <div class="ov-toolbar">
      <span class="ov-title">数据概览</span>
      <span v-if="stats?.firstAt" class="ov-range">数据范围 {{ fmtTime(stats.firstAt) }} ~ {{ fmtTime(stats.lastAt) }}</span>
      <a-button size="small" :loading="loading" @click="load"><template #icon><ReloadOutlined /></template>刷新</a-button>
    </div>

    <div class="ov-grid">
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--brand-tint); color: var(--brand)"><MessageOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.conversations ?? 0 }}</div>
          <div class="ov-label">会话总数</div>
          <div class="ov-extra">{{ stats?.users ?? 0 }} 个用户</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--ok-tint); color: var(--ok)"><CommentOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.messages ?? 0 }}</div>
          <div class="ov-label">消息数</div>
          <div class="ov-extra">{{ stats?.events ?? 0 }} 条事件</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--violet-tint); color: var(--violet)"><RobotOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.llm ?? 0 }}</div>
          <div class="ov-label">LLM 调用</div>
          <div class="ov-extra">平均 {{ stats?.calls.avgDurationMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--cyan-tint); color: var(--cyan)"><CloudServerOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.upstream ?? 0 }}</div>
          <div class="ov-label">上游调用</div>
          <div class="ov-extra">合计 {{ stats?.calls.upstreamMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card" v-if="(stats?.calls?.byType?.graph?.count ?? 0) > 0">
        <div class="ov-icon" style="background: var(--brand-tint); color: var(--brand)"><NodeIndexOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.graph?.count ?? 0 }}</div>
          <div class="ov-label">图谱检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.graph?.totalMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card" v-if="(stats?.calls?.byType?.vector?.count ?? 0) > 0">
        <div class="ov-icon" style="background: var(--ok-tint); color: var(--ok)"><DatabaseOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.vector?.count ?? 0 }}</div>
          <div class="ov-label">向量检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.vector?.totalMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card" v-if="(stats?.calls?.byType?.fulltext?.count ?? 0) > 0">
        <div class="ov-icon" style="background: var(--orange-tint); color: var(--orange)"><FileSearchOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.fulltext?.count ?? 0 }}</div>
          <div class="ov-label">全文检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.fulltext?.totalMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--orange-tint); color: var(--orange)"><AppstoreOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.packs?.enabled ?? 0 }}<span class="ov-dim">/​{{ stats?.packs?.discovered ?? 0 }}</span></div>
          <div class="ov-label">启用插件</div>
          <div class="ov-extra">热切换即时生效</div>
        </div>
      </div>
      <div class="ov-card">
        <div class="ov-icon" style="background: var(--pink-tint); color: var(--pink)"><NodeIndexOutlined /></div>
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
import { computed, inject, onMounted, ref } from 'vue'
import {AppstoreOutlined, CloudServerOutlined, CommentOutlined, DatabaseOutlined, FileSearchOutlined, MessageOutlined, NodeIndexOutlined, ReloadOutlined, RobotOutlined} from '@ant-design/icons-vue'
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
/* 精装层(2026-11 重设计):令牌见 shared/design-tokens.css,规格对齐 design-demos/polished-v1.html */
.ov-toolbar { display: flex; align-items: center; gap: 14px; margin: 16px 0 18px; }
.ov-title { font-size: 18px; font-weight: 650; color: var(--ink); letter-spacing: .01em; }
.ov-range { flex: 1; color: var(--ink-3); font-size: 14px; font-variant-numeric: tabular-nums; }
.ov-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
}
.ov-card {
  display: flex; gap: 14px; align-items: center;
  border: 1px solid var(--line-soft); border-radius: var(--radius-card); padding: 20px 18px;
  background: var(--card);
  box-shadow: var(--shadow-rest);
  transition: box-shadow 0.18s, transform 0.18s, border-color 0.18s;
}
.ov-card:hover {
  box-shadow: var(--shadow-lift);
  transform: translateY(-2px);
  border-color: #DFE3EA;
}
.ov-icon {
  width: 44px; height: 44px; border-radius: 10px; font-size: 20px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.ov-value { font-size: 26px; font-weight: 700; color: var(--ink); line-height: 1.1; letter-spacing: -.01em; font-variant-numeric: tabular-nums; }
.ov-dim { font-size: 16px; color: var(--ink-3); font-weight: 500; }
.ov-label { font-size: 13.5px; color: var(--ink-2); margin-top: 4px; }
.ov-extra { font-size: 12.5px; color: var(--ink-3); margin-top: 3px; font-variant-numeric: tabular-nums; }
.ov-ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 150px; }
</style>
