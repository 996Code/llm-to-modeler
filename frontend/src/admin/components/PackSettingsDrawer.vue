<template>
  <a-drawer
    :open="open" width="560" :title="`插件设置 · ${packName}`"
    @close="$emit('update:open', false)"
  >
    <a-spin :spinning="loading">
      <template v-if="payload">
        <a-alert type="info" show-icon style="margin-bottom: 16px">
          <template #message>配置优先级:此处保存值 &gt; 环境变量 &gt; 默认值</template>
          <template #description>
            密钥类字段已配置时不回显,留空提交表示保持不变;清空内容提交表示清除
            (回落到环境变量/默认值)。
          </template>
        </a-alert>

        <a-form layout="vertical" class="ps-form">
          <div v-for="group in payload.schema.groups" :key="group.key" class="ps-group">
            <div class="ps-group-title">{{ group.title }}</div>

            <a-form-item
              v-for="field in group.fields" :key="field.key"
              :label="fieldLabel(field)"
              :extra="fieldHelp(field)"
            >
              <!-- 布尔开关 -->
              <a-switch v-if="field.type === 'bool'" v-model:checked="form[field.key]" />

              <!-- 枚举 -->
              <a-select
                v-else-if="field.type === 'enum'"
                v-model:value="form[field.key]"
                :placeholder="field.placeholder || '请选择'"
                allow-clear
              >
                <a-select-option v-for="opt in field.options || []" :key="String(opt)" :value="opt">
                  {{ String(opt) }}
                </a-select-option>
              </a-select>

              <!-- 密钥:已配置时不回显 -->
              <a-input-password
                v-else-if="field.type === 'secret'"
                v-model:value="form[field.key]"
                :placeholder="secretPlaceholder(field)"
              />

              <!-- 整数 -->
              <a-input-number
                v-else-if="field.type === 'int'"
                v-model:value="form[field.key]"
                :min="field.min" :max="field.max"
                :placeholder="field.placeholder || String(field.default ?? '')"
                style="width: 100%"
              />

              <!-- 字符串 -->
              <a-input
                v-else
                v-model:value="form[field.key]"
                :placeholder="field.placeholder || String(field.default ?? '')"
                allow-clear
              />
            </a-form-item>
          </div>
        </a-form>

        <div class="ps-footer">
          <a-button @click="$emit('update:open', false)">取消</a-button>
          <a-button type="primary" :loading="saving" @click="save">保存</a-button>
        </div>
      </template>
      <a-empty v-else-if="!loading" description="加载失败或插件未声明配置页" style="padding: 60px 0" />
    </a-spin>
  </a-drawer>
</template>

<script setup lang="ts">
// 插件设置抽屉:settings.schema.yaml 驱动的通用表单(string/int/bool/enum/secret)。
// secret 掩码哨兵 __SET__ 的语义:显示"已配置"占位,留空提交 = 保持不变。
import { inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  PackSettingsPayload, SettingsField, fetchPackSettings, savePackSettings,
} from '../api'
import type { LoadSafely } from './loadSafely'

const SET = '__SET__'
const loadSafely = inject<LoadSafely>('loadSafely')!

const props = defineProps<{ open: boolean; packName: string }>()
const emit = defineEmits<{
  (e: 'update:open', v: boolean): void
  (e: 'saved'): void
}>()

const payload = ref<PackSettingsPayload | null>(null)
const form = ref<Record<string, unknown>>({})
const loading = ref(false)
const saving = ref(false)
/** 打开时拍下的初始快照(save 时做差集,只提交变化键) */
let baseline: Record<string, unknown> = {}

watch(() => props.open, async (open) => {
  if (!open || !props.packName) return
  loading.value = true
  await loadSafely(async () => {
    payload.value = await fetchPackSettings(props.packName)
    form.value = { ...payload.value.values }
    baseline = { ...payload.value.values }
  })
  loading.value = false
})

function fieldLabel(f: SettingsField): string {
  return f.required ? `${f.label} *` : f.label
}
function fieldHelp(f: SettingsField): string {
  const parts: string[] = []
  if (f.env) parts.push(`环境变量兜底: ${f.env}`)
  if (f.help) parts.push(f.help)
  return parts.join(' · ')
}
function secretPlaceholder(f: SettingsField): string {
  return payload.value?.values[f.key] === SET ? '已配置(不回显),留空保持不变' : (f.placeholder || '未配置')
}

/** form 与 baseline 的差集:值有变 / 新出现的键才进提交体。 */
function buildDelta(): Record<string, unknown> {
  const delta: Record<string, unknown> = {}
  const keys = new Set([...Object.keys(form.value), ...Object.keys(baseline)])
  for (const key of keys) {
    const cur = form.value[key]
    const old = baseline[key]
    if (cur === old) continue
    // 空串 → null(显式清除,回落 env/默认);secret 哨兵 SET 原样传,后端跳过
    delta[key] = cur === '' ? null : cur
  }
  return delta
}

async function save() {
  if (!payload.value) return
  const delta = buildDelta()
  if (!Object.keys(delta).length) {
    message.info('没有需要保存的变更')
    return
  }
  saving.value = true
  await loadSafely(async () => {
    const saved = await savePackSettings(props.packName, delta)
    payload.value = saved
    form.value = { ...saved.values }
    baseline = { ...saved.values }
    const dep = saved.dependency
    if (dep && dep.status !== 'ok') {
      message.warning(`已保存,但依赖仍不满足:${dep.detail}(可点插件卡上的"重新检测")`)
    } else {
      message.success('配置已保存')
    }
    emit('saved')
  })
  saving.value = false
}
</script>

<style scoped>
.ps-group { margin-bottom: 18px; }
.ps-group-title {
  font-weight: 600; font-size: 13px; color: #1f2937;
  border-left: 3px solid #2f54eb; padding-left: 8px; margin-bottom: 12px;
}
.ps-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 8px; }
</style>
