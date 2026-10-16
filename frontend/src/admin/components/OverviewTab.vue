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
      <div class="ov-card" v-if="(stats?.calls?.byType?.graph?.count ?? 0) > 0">
        <div class="ov-icon" style="background: #eff6ff; color: #2563eb"><NodeIndexOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.graph?.count ?? 0 }}</div>
          <div class="ov-label">图谱检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.graph?.totalMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card" v-if="(stats?.calls?.byType?.vector?.count ?? 0) > 0">
        <div class="ov-icon" style="background: #f0fdf4; color: #16a34a"><DatabaseOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.vector?.count ?? 0 }}</div>
          <div class="ov-label">向量检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.vector?.totalMs ?? 0 }} ms</div>
        </div>
      </div>
      <div class="ov-card" v-if="(stats?.calls?.byType?.fulltext?.count ?? 0) > 0">
        <div class="ov-icon" style="background: #fff7ed; color: #ea580c"><FileSearchOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">{{ stats?.calls.byType?.fulltext?.count ?? 0 }}</div>
          <div class="ov-label">全文检索</div>
          <div class="ov-extra">合计 {{ stats?.calls.byType?.fulltext?.totalMs ?? 0 }} ms</div>
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
      <div class="ov-card">
        <div class="ov-icon" style="background: #fefce8; color: #ca8a04"><DollarOutlined /></div>
        <div class="ov-body">
          <div class="ov-value">¥{{ totalCostText }}</div>
          <div class="ov-label">近 7 天 LLM 费用</div>
          <div class="ov-extra ov-ellipsis" :title="costExtra">{{ costExtra }}</div>
        </div>
        <a-button size="small" type="text" class="ov-card-action" @click="openPricing">配置单价</a-button>
      </div>
    </div>

    <!-- P2#13 单价配置:费用卡入口弹层(PUT 全量替换语义,条目 {input_per_1k, output_per_1k} 元/千 token) -->
    <a-modal v-model:open="pricingOpen" title="模型单价配置(元/千 token)"
             :confirm-loading="pricingSaving" @ok="savePricing" ok-text="保存" cancel-text="取消">
      <p class="pricing-hint">按 call_logs 实际出站模型名配置;保存为全量替换。费用 = tokens × 单价。</p>
      <div v-for="(row, i) in pricingRows" :key="i" class="pricing-row">
        <a-input v-model:value="row.model" placeholder="模型名(如 kimi-k3-oc)" style="width: 40%" />
        <a-input-number v-model:value="row.input_per_1k" :min="0" :step="0.001"
                        placeholder="输入单价" style="width: 26%" />
        <a-input-number v-model:value="row.output_per_1k" :min="0" :step="0.001"
                        placeholder="输出单价" style="width: 26%" />
        <a-button size="small" type="text" danger @click="pricingRows.splice(i, 1)">删</a-button>
      </div>
      <a-button size="small" type="dashed" block @click="pricingRows.push({ model: '', input_per_1k: null, output_per_1k: null })">
        + 添加模型
      </a-button>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 概览:图标化统计卡片,数据结构与 backend get_admin_stats / get_cost_summary 对齐。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  MessageOutlined, CommentOutlined, RobotOutlined, CloudServerOutlined,
  AppstoreOutlined, NodeIndexOutlined, DatabaseOutlined, DollarOutlined,
  FileSearchOutlined,
} from '@ant-design/icons-vue'
import { AdminStats, apiErrorMessage, CostSummary, fetchCostSummary,
         fetchModelPricing, fetchStats, fmtTime, saveModelPricing } from '../api'
import type { ModelPricingEntry } from '../api'
import type { LoadSafely } from './loadSafely'

const stats = ref<AdminStats | null>(null)
const costSummary = ref<CostSummary | null>(null)
const loading = ref(false)
const loadSafely = inject<LoadSafely>('loadSafely')!

// 近 7 天总费用(有价模型合计;未配单价时为 0,extra 提示补配)
const totalCostText = computed(() =>
  (costSummary.value?.total_cost ?? 0).toFixed(2))

// 按 pack 汇总的费用小列表(取前 3,超长省略;title 悬停看全量)
const byPack = computed(() => {
  const m = new Map<string, number>()
  for (const it of costSummary.value?.items ?? []) {
    if (it.cost == null) continue
    const key = it.pack || '(无归属)'
    m.set(key, (m.get(key) ?? 0) + it.cost)
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1])
})

const costExtra = computed(() => {
  if (!costSummary.value) return ''
  if (!costSummary.value.items.length) return '近 7 天无 LLM 调用'
  if (!byPack.value.length) return '未配置模型单价(费用显示为 0)'
  return byPack.value.slice(0, 3)
    .map(([pack, cost]) => `${pack} ¥${cost.toFixed(2)}`)
    .join(' · ')
})

// ── P2#13 单价配置(费用卡入口) ──────────────────────────
interface PricingRow { model: string; input_per_1k: number | null; output_per_1k: number | null }
const pricingOpen = ref(false)
const pricingSaving = ref(false)
const pricingRows = ref<PricingRow[]>([])

async function openPricing() {
  await loadSafely(async () => {
    const payload = await fetchModelPricing()
    pricingRows.value = Object.entries(payload.models ?? {}).map(([model, e]) => ({
      model, input_per_1k: e.input_per_1k, output_per_1k: e.output_per_1k,
    }))
    if (!pricingRows.value.length) {
      pricingRows.value = [{ model: '', input_per_1k: null, output_per_1k: null }]
    }
    pricingOpen.value = true
  })
}

async function savePricing() {
  const models: Record<string, ModelPricingEntry> = {}
  for (const r of pricingRows.value) {
    const name = r.model.trim()
    if (!name) continue
    if (r.input_per_1k == null || r.output_per_1k == null) {
      alert(`模型 ${name} 的输入/输出单价需完整`)
      return
    }
    models[name] = { input_per_1k: r.input_per_1k, output_per_1k: r.output_per_1k }
  }
  pricingSaving.value = true
  try {
    await saveModelPricing(models)
    pricingOpen.value = false
    await load()
  } catch (e) {
    message.error(apiErrorMessage(e))
  } finally { pricingSaving.value = false }
}

async function load() {
  loading.value = true
  await loadSafely(async () => {
    stats.value = await fetchStats()
    // 费用卡片独立拉取:单价未配/聚合失败不拖垮概览主统计
    try {
      costSummary.value = await fetchCostSummary(7)
    } catch { /* 费用卡片缺数据不影响其他卡片 */ }
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
.ov-card-action { align-self: flex-start; margin-left: auto; flex-shrink: 0; }
.pricing-hint { color: #888; font-size: 12px; margin: 0 0 10px; }
.pricing-row { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
.ov-icon {
  width: 44px; height: 44px; border-radius: 10px; font-size: 20px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.ov-value { font-size: 24px; font-weight: 700; color: #111827; line-height: 1.1; }
.ov-dim { font-size: 14px; color: #9ca3af; font-weight: 500; }
.ov-label { font-size: 13px; color: #4b5563; margin-top: 3px; }
.ov-extra { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.ov-ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 150px; }
</style>
