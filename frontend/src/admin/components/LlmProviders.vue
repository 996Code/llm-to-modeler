<template>
  <!-- 平台 LLM 供应商维护(C8/设计 8.6):主/备两组表单,revision CAS,
       密钥不回显(留空=沿用),切换策略只读,可选连通性探测。 -->
  <div class="lp">
    <div class="lp-head">
      <div>
        <div class="lp-title">模型供应商</div>
        <div class="lp-sub">
          DB 唯一事实源,保存即热生效(下一次 LLM 调用读到新配置)。
          主端点<b>连接类失败/超时/5xx</b>时同参数自动切备用;
          <b>身份(401)/参数(400)/限流(429)不切</b>——换端点不解决问题。
          每次调用的 call_logs 记 provider=primary/fallback 与成本(单价已配时)。
        </div>
      </div>
      <a-tag :color="dirty ? 'warning' : 'green'">{{ dirty ? '有未保存修改' : `revision ${revision}` }}</a-tag>
    </div>

    <div class="lp-grid">
      <section class="lp-card">
        <header class="lp-card-head">
          <strong>主供应商</strong>
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
        <div v-else class="lp-empty" @click="addingFallback = true; dirty = true">
          + 配置备用供应商(可选——主端点故障时自动接管)
        </div>
      </section>
    </div>

    <div class="lp-foot">
      <a-space>
        <a-button type="primary" :disabled="!dirty" :loading="saving" @click="save">保存（revision {{ revision }} → {{ revision + 1 }}）</a-button>
        <a-button :disabled="!dirty" @click="reload">放弃修改</a-button>
        <a-button :loading="probing" @click="probe">连通性探测</a-button>
      </a-space>
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
// 模型供应商维护(C8):GET 掩码(has_key)→ 表单;PUT 留空 key=沿用旧值;
// revision CAS(409 提示刷新);探测只读不改配置。
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { adminApi } from '../api'

interface Role {
  base_url: string; api_key: string; model: string
  enabled: boolean; price_in: string | null; price_out: string | null
}
function emptyRole(): Role {
  return { base_url: '', api_key: '', model: '', enabled: true, price_in: null, price_out: null }
}

const revision = ref(0)
const form = reactive<{ primary: Role; fallback: Role }>({ primary: emptyRole(), fallback: emptyRole() })
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
  revision.value = data.revision
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
    const body: any = {
      primary: { ...form.primary },
      expected_revision: revision.value,
    }
    if (addingFallback.value) body.fallback = { ...form.fallback }
    const { data } = await adminApi.put('/llm-providers', body)
    message.success(`已保存 revision ${data.revision},下一次 LLM 调用生效`)
    await reload()
  } catch (e: any) {
    if (e?.response?.status === 409) errorText.value = '配置已被他人修改(409),请「放弃修改」后重试'
    else if (e?.response?.status === 400) errorText.value = `校验失败: ${e.response.data?.detail || ''}`
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
.lp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.lp-card { border: 1px solid #e3e7ee; border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.lp-card-head { display: flex; justify-content: space-between; align-items: center; }
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
</style>
