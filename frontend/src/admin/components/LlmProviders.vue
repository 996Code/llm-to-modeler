<template>
  <!-- 密钥不回显；留空时沿用已保存的值。 -->
  <div class="lp">
    <div v-if="!embedded" class="lp-head">
      <div class="lp-title">模型供应商</div>
    </div>

    <div class="lp-grid">
      <section class="lp-card">
        <header class="lp-card-head">
          <div class="lp-provider-heading">
            <strong>主供应商</strong>
            <a-badge :status="configured ? 'success' : 'default'" :text="configured ? '已配置' : '未配置'" />
          </div>
          <a-switch v-model:checked="form.primary.enabled" checked-children="启用" un-checked-children="停用" size="small" @change="dirty = true" />
        </header>
        <label class="lp-field"><span>Base URL</span><input v-model="form.primary.base_url" placeholder="https://api.example.com/v1" @input="dirty = true" /></label>
        <label class="lp-field"><span>API Key <em class="lp-hint">{{ hasPrimaryKey ? '已配置(留空=不修改)' : '未配置' }}</em></span>
          <input v-model="form.primary.api_key" type="password" placeholder="留空 = 沿用已存密钥" @input="dirty = true" /></label>
        <label class="lp-field"><span>模型</span><input v-model="form.primary.model" placeholder="qwen3.7-plus" @input="dirty = true" /></label>
        <div class="lp-row">
          <label class="lp-field"><span>输入单价(元/千token)</span><input v-model="form.primary.price_in" placeholder="可空" @input="dirty = true" /></label>
          <label class="lp-field"><span>输出单价(元/千token)</span><input v-model="form.primary.price_out" placeholder="可空" @input="dirty = true" /></label>
        </div>
      </section>
    </div>

    <div class="lp-foot">
      <a-space>
        <a-button type="primary" :disabled="!dirty" :loading="saving" @click="save"><template #icon><SaveOutlined /></template>保存</a-button>
        <a-button :disabled="!dirty" @click="reload"><template #icon><UndoOutlined /></template>放弃修改</a-button>
        <a-button :loading="probing" @click="probe"><template #icon><ApiOutlined /></template>检测连接</a-button>
      </a-space>
      <span v-if="dirty" class="lp-pending" role="status">有未保存修改</span>
      <span v-if="errorText" class="lp-error">{{ errorText }}</span>
    </div>

    <div v-if="probeResults" class="lp-probe">
      <div v-for="r in probeResults" :key="r.role" class="lp-probe-row" :class="r.ok ? 'ok' : 'no'">
        <span>{{ r.role }} · {{ r.base_url }}</span>
        <span>{{ r.ok ? `可达 ${r.latency_ms}ms` : `不可达: ${r.error || '未知错误'}` }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// embedded 用于集成页内嵌，避免重复标题。
import { onMounted, reactive, ref } from 'vue'
import { ApiOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { adminApi } from '../api'

defineProps<{ embedded?: boolean }>()

interface Role {
  base_url: string; api_key: string; model: string
  enabled: boolean; price_in: string | null; price_out: string | null
}
function emptyRole(): Role {
  return { base_url: '', api_key: '', model: '', enabled: true, price_in: null, price_out: null }
}

const form = reactive<{ primary: Role }>({ primary: emptyRole() })
const configured = ref(false)
const hasPrimaryKey = ref(false)
const dirty = ref(false)
const saving = ref(false)
const probing = ref(false)
const errorText = ref('')
const probeResults = ref<any[] | null>(null)

async function reload() {
  const { data } = await adminApi.get('/llm-providers')
  configured.value = !!data.configured
  const mapIn = (r: any): Role => ({
    base_url: r?.base_url || '', api_key: '',
    model: r?.model || '', enabled: r?.enabled ?? true,
    price_in: r?.price_in ?? null, price_out: r?.price_out ?? null,
  })
  form.primary = mapIn(data.primary)
  hasPrimaryKey.value = !!data.primary?.has_key
  dirty.value = false
  errorText.value = ''
}

async function save() {
  saving.value = true
  errorText.value = ''
  try {
    const body: any = { primary: { ...form.primary } }
    await adminApi.put('/llm-providers', body)
    message.success('已保存,下一次 LLM 调用生效')
    await reload()
  } catch (e: any) {
    if (e?.response?.status === 400) errorText.value = `校验失败: ${e.response.data?.detail || ''}`
    else errorText.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

async function probe() {
  probing.value = true
  probeResults.value = null
  try {
    const { data } = await adminApi.post('/llm-providers/probe')
    probeResults.value = data.results
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '探测失败')
  } finally {
    probing.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
/* 精装层(2026-11 重设计):令牌见 shared/design-tokens.css,规格对齐 design-demos/polished-packs.html */
.lp { display: flex; flex-direction: column; gap: 16px; }
.lp-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.lp-title { font-size: 16px; font-weight: 600; color: var(--ink); }
.lp-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.lp-card {
  min-width: 0; max-width: 640px;
  border: 1px solid var(--line-soft); border-radius: var(--radius-card);
  box-shadow: var(--shadow-rest); background: var(--card);
  padding: 18px 20px; display: flex; flex-direction: column; gap: 14px;
  transition: box-shadow .18s;
}
.lp-card:hover { box-shadow: var(--shadow-lift); }
.lp-card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.lp-provider-heading { display: flex; align-items: center; gap: 10px; font-size: 15px; font-weight: 650; color: var(--ink); }
.lp-provider-heading :deep(.ant-badge-status-text) { color: var(--ink-3); font-size: 12px; }
.lp-pending { color: var(--warn); font-size: 12px; }
.lp-field { display: flex; flex-direction: column; gap: 5px; font-size: 13px; color: var(--ink-2); }
/* 原生 input 不在 admin.css 的 .ant-input focus 选择器内,这里补齐同一款 focus 态(品牌蓝描边+淡蓝光环) */
.lp-field input {
  border: 1px solid var(--line); border-radius: var(--radius-ctrl);
  padding: 8px 12px; font-size: 14px; color: var(--ink); background: var(--card);
  min-width: 0; width: 100%;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  transition: border-color .15s, box-shadow .15s;
}
.lp-field input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-tint); }
.lp-hint { font-style: normal; color: var(--ink-3); margin-left: 6px; }
.lp-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.lp-foot { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; border-top: 1px solid var(--line-soft); padding-top: 14px; }
.lp-error { color: var(--danger); font-size: 12px; }
.lp-probe { display: flex; flex-direction: column; gap: 6px; }
.lp-probe-row {
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px;
  font-size: 13px; color: var(--ink-2);
  padding: 9px 12px; border-radius: var(--radius-ctrl); background: var(--bg);
  overflow-wrap: anywhere;
}
.lp-probe-row.ok span:last-child { color: var(--ok); font-weight: 500; }
.lp-probe-row.no span:last-child { color: var(--danger); }
@media (max-width: 700px) {
  .lp-grid { grid-template-columns: minmax(0, 1fr); }
  .lp-row { grid-template-columns: minmax(0, 1fr); }
}
</style>
