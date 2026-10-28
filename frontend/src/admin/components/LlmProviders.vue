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

      <section class="lp-card">
        <header class="lp-card-head">
          <strong>备用供应商</strong>
          <a-switch v-model:checked="form.fallback.enabled" :disabled="!hasFallback" checked-children="启用" un-checked-children="停用" size="small" @change="dirty = true" />
        </header>
        <template v-if="hasFallback || addingFallback">
          <label class="lp-field"><span>Base URL</span><input v-model="form.fallback.base_url" @input="dirty = true" /></label>
          <label class="lp-field"><span>API Key <em class="lp-hint">{{ hasFallbackKey ? '已配置(留空=不修改)' : '未配置' }}</em></span>
            <input v-model="form.fallback.api_key" type="password" placeholder="留空 = 沿用已存密钥" @input="dirty = true" /></label>
          <label class="lp-field"><span>模型</span><input v-model="form.fallback.model" @input="dirty = true" /></label>
          <div class="lp-row">
            <label class="lp-field"><span>输入单价(元/千token)</span><input v-model="form.fallback.price_in" placeholder="可空" @input="dirty = true" /></label>
            <label class="lp-field"><span>输出单价(元/千token)</span><input v-model="form.fallback.price_out" placeholder="可空" @input="dirty = true" /></label>
          </div>
        </template>
        <a-button v-else class="lp-empty" @click="addingFallback = true; dirty = true">
          <template #icon><PlusOutlined /></template>添加备用供应商
        </a-button>
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
import { ApiOutlined, PlusOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons-vue'
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

const form = reactive<{ primary: Role; fallback: Role }>({ primary: emptyRole(), fallback: emptyRole() })
const configured = ref(false)
const hasPrimaryKey = ref(false)
const hasFallbackKey = ref(false)
const hasFallback = ref(false)
const addingFallback = ref(false)
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
  if (data.fallback) {
    form.fallback = mapIn(data.fallback)
    hasFallbackKey.value = !!data.fallback.has_key
    hasFallback.value = true
    addingFallback.value = true
  } else {
    form.fallback = emptyRole()
    hasFallbackKey.value = false
    hasFallback.value = false
    addingFallback.value = false
  }
  dirty.value = false
  errorText.value = ''
}

async function save() {
  saving.value = true
  errorText.value = ''
  try {
    const body: any = { primary: { ...form.primary } }
    if (addingFallback.value) body.fallback = { ...form.fallback }
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
.lp { display: flex; flex-direction: column; gap: 16px; }
.lp-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.lp-title { font-size: 16px; font-weight: 600; }
.lp-sub { color: #7a8491; font-size: 12px; margin-top: 6px; max-width: 680px; line-height: 1.8; }
.lp-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.lp-card { min-width: 0; border-top: 1px solid #e3e7ee; padding: 16px 0; display: flex; flex-direction: column; gap: 16px; }
.lp-card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.lp-provider-heading { display: flex; align-items: center; gap: 10px; }
.lp-provider-heading :deep(.ant-badge-status-text) { color: #86909c; font-size: 12px; }
.lp-pending { color: #ad6800; font-size: 12px; }
.lp-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #4a5568; }
.lp-field input { border: 1px solid #d9dde3; border-radius: 6px; padding: 6px 10px; font-size: 13px; }
.lp-hint { font-style: normal; color: #9aa4b2; margin-left: 6px; }
.lp-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.lp-empty { border: 1px dashed #d9dde3; border-radius: 8px; padding: 18px; text-align: center; color: #7a8491; cursor: pointer; }
.lp-empty:hover { border-color: #91caff; color: #1677ff; }
.lp-foot { display: flex; align-items: center; gap: 12px; border-top: 1px solid #edf0f3; padding-top: 12px; }
.lp-error { color: #d4380d; font-size: 12px; }
.lp-probe { display: flex; flex-direction: column; gap: 6px; }
.lp-probe-row { display: flex; justify-content: space-between; font-size: 13px; padding: 8px 12px; border-radius: 6px; background: #f6f7f9; }
.lp-probe-row.ok span:last-child { color: #389e0d; }
.lp-probe-row.no span:last-child { color: #d4380d; }
.lp-field input { min-width: 0; width: 100%; }
.lp-row { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.lp-foot, .lp-probe-row { flex-wrap: wrap; gap: 12px; }
.lp-probe-row { overflow-wrap: anywhere; }
@media (max-width: 700px) { .lp-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
