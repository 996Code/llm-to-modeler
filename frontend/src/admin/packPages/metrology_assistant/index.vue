<template>
  <!-- 计量客服管理页:场景与槽位 / 规则与来源 两个平级 Tab(设计 §9 Tab1/Tab2)。
       DB 唯一事实源:整档加载 → 表单编辑 → 整档保存(revision CAS)。
       Tab3 联调预演按设计 0.5.1 后移 P2,本页不占位。 -->
  <div class="metro-admin">
    <div class="metro-admin-head">
      <div>
        <div class="metro-admin-title">计量智能客服 · 场景配置</div>
        <div class="metro-admin-sub">
          场景/槽位/规则/来源的唯一事实源在数据库——改场景是管理页操作,不是改代码;
          保存即校验+热生效(下一次对话读到新配置)。
        </div>
      </div>
      <div class="metro-admin-meta">
        <a-tag v-if="config.factory" color="orange">出厂默认(未保存过)</a-tag>
        <a-tag v-else :color="dirty ? 'warning' : 'green'">
          {{ dirty ? '有未保存修改' : `revision ${config.revision}` }}
        </a-tag>
      </div>
    </div>

    <a-tabs v-model:activeKey="tab" size="large" class="flat-tabs">
      <a-tab-pane key="scenarios">
        <template #tab><ProfileOutlined /> 场景与槽位</template>
        <div class="metro-tab-body">
          <div class="scenario-list">
            <div
              v-for="(cfg, key) in draft.scenarios" :key="key"
              class="scenario-item" :class="{ active: currentKey === key }"
              @click="currentKey = key"
            >
              <div class="scenario-item-name">{{ key }}</div>
              <div class="scenario-item-label">{{ cfg.label }}</div>
            </div>
          </div>
          <div v-if="currentScenario" class="scenario-editor">
            <div class="field-grid">
              <label class="field">
                <span>场景 key(路由标识)</span>
                <input :value="currentKey" disabled />
              </label>
              <label class="field">
                <span>显示名</span>
                <input v-model="currentScenario.label" @input="dirty = true" />
              </label>
            </div>
            <label class="field">
              <span>意图描述(提取提示词自动拼装)</span>
              <textarea v-model="currentScenario.intent" rows="2" @input="dirty = true" />
            </label>
            <fieldset class="slot-box">
              <legend>必填槽位与追问</legend>
              <div class="field-grid">
                <label class="field">
                  <span>槽位 key</span>
                  <input :value="currentScenario.required.slot" disabled />
                </label>
                <label class="field">
                  <span>追问 header(≤12 字)</span>
                  <input v-model="currentScenario.required.header" @input="dirty = true" />
                </label>
              </div>
              <label class="field">
                <span>追问文案({subject}=器具名占位)</span>
                <textarea v-model="currentScenario.required.question" rows="2" @input="dirty = true" />
              </label>
              <label class="field">
                <span>追问选项(label, description;每行一项)</span>
                <textarea
                  :value="optionsText(currentScenario)"
                  rows="4" @input="setOptions(currentScenario, $event); dirty = true"
                />
              </label>
            </fieldset>
          </div>
        </div>
      </a-tab-pane>

      <a-tab-pane key="actions">
        <template #tab><ApiOutlined /> 规则与来源</template>
        <div class="metro-tab-body">
          <div v-if="currentScenario" class="scenario-editor">
            <div class="field-grid">
              <label class="field">
                <span>动作类型</span>
                <select v-model="currentScenario.action.kind" @change="dirty = true">
                  <option value="kg">kg(知识库证据检索)</option>
                  <option value="chatbi">chatbi(问数受控执行)</option>
                </select>
              </label>
              <template v-if="currentScenario.action.kind === 'chatbi'">
                <label class="field">
                  <span>数据源 env 变量</span>
                  <input v-model="currentScenario.action.env" @input="dirty = true" />
                </label>
                <label class="field">
                  <span>数据源显示名</span>
                  <input v-model="currentScenario.action.ds_name" @input="dirty = true" />
                </label>
              </template>
            </div>
            <template v-if="currentScenario.action.kind === 'chatbi'">
              <div class="field-grid">
                <label class="field">
                  <span>固定工具(白名单)</span>
                  <input v-model="currentScenario.action.tool" @input="dirty = true" />
                </label>
                <label class="field">
                  <span>结果卡构建器</span>
                  <input :value="currentScenario.action.card" disabled />
                </label>
              </div>
              <label class="field">
                <span>工具参数 ← 槽位映射(JSON:工具参数名→槽位 key)</span>
                <textarea
                  :value="paramsText(currentScenario)"
                  rows="2" @input="setParams(currentScenario, $event); dirty = true"
                />
              </label>
              <label class="field">
                <span>业务状态通知文案(JSON:status→{title, message})</span>
                <textarea
                  :value="noticesText(currentScenario)"
                  rows="4" @input="setNotices(currentScenario, $event); dirty = true"
                />
              </label>
            </template>
            <a-alert v-else type="info" show-icon class="kg-hint">
              <template #message>kg 动作:按用途选库(强检目录/检定规程)→ 证据检索 → 三态结论;来源绑定由知识库侧维护,此处无数据源参数。</template>
            </a-alert>
          </div>
        </div>
      </a-tab-pane>
    </a-tabs>

    <div class="metro-admin-foot">
      <a-space>
        <a-button :disabled="!dirty" :loading="saving" type="primary" @click="save">
          保存(revision {{ config.revision }} → {{ config.revision + 1 }})
        </a-button>
        <a-button :disabled="!dirty" @click="reload">放弃修改</a-button>
        <a-button @click="showHistory = true">历史与回滚</a-button>
      </a-space>
      <span v-if="saveError" class="save-error">{{ saveError }}</span>
    </div>

    <a-drawer v-model:open="showHistory" title="配置历史(只增不改;回滚=旧快照生成新 revision)" width="480">
      <a-list :data-source="history" size="small">
        <template #renderItem="{ item }">
          <a-list-item>
            <a-list-item-meta>
              <template #title>
                revision {{ item.revision }}
                <a-tag v-if="item.revision === config.revision" color="green" class="hist-tag">当前</a-tag>
              </template>
              <template #description>
                {{ item.updatedAt }} · {{ item.updatedBy || '-' }} · {{ item.comment || '无备注' }}
              </template>
            </a-list-item-meta>
            <template #actions>
              <a-popconfirm
                title="回滚到该版本?当前修改将丢失" ok-text="回滚" ok-type="danger"
                @confirm="rollback(item.revision)"
              >
                <a-button size="small" :disabled="item.revision === config.revision">回滚</a-button>
              </a-popconfirm>
            </template>
          </a-list-item>
        </template>
      </a-list>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 计量客服管理页 —— 场景配置两 tab 编排壳:
//   场景与槽位(意图/追问文案/选项) + 规则与来源(动作绑定/通知文案)。
// 保存语义:整档提交 + revision 乐观锁(409 提示刷新)+ 历史留痕 + 回滚。
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ApiOutlined, ProfileOutlined } from '@ant-design/icons-vue'
import { metrologyApi, type MetrologyConfig, type MetrologyHistoryItem, type MetrologyScenario } from '../../metrologyApi'

const tab = ref('scenarios')
const currentKey = ref('')
const config = ref<MetrologyConfig>({ revision: 0, factory: true, content: { scenarios: {} } })
const draft = ref<{ scenarios: Record<string, MetrologyScenario> }>({ scenarios: {} })
const dirty = ref(false)
const saving = ref(false)
const saveError = ref('')
const showHistory = ref(false)
const history = ref<MetrologyHistoryItem[]>([])

const currentScenario = computed(() => draft.value.scenarios[currentKey.value])
const scenarioKeys = computed(() => Object.keys(draft.value.scenarios))

async function reload() {
  const { data } = await metrologyApi.get<MetrologyConfig>('/config')
  config.value = data
  draft.value = JSON.parse(JSON.stringify(data.content))
  currentKey.value = scenarioKeys.value[0] || ''
  dirty.value = false
  saveError.value = ''
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const { data } = await metrologyApi.put('/config', {
      content: draft.value,
      expected_revision: config.value.revision,
      comment: `管理页编辑(${scenarioKeys.value.length} 个场景)`,
    })
    message.success(`已保存 revision ${data.revision},下一次对话生效`)
    await reload()
  } catch (e: any) {
    if (e?.response?.status === 409) saveError.value = '配置已被他人修改(409),请点「放弃修改」后重试'
    else if (e?.response?.status === 400) saveError.value = `校验失败: ${e.response.data?.detail || ''}`
    else saveError.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

async function loadHistory() {
  const { data } = await metrologyApi.get<{ items: MetrologyHistoryItem[] }>('/config/history')
  history.value = data.items
}

async function rollback(toRevision: number) {
  try {
    const { data } = await metrologyApi.post('/config/rollback', { to_revision: toRevision })
    message.success(`已回滚(新 revision ${data.revision})`)
    await reload()
    await loadHistory()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '回滚失败')
  }
}

// 选项/参数/通知:JSON 文本 ↔ 结构(表单友好;非法 JSON 保存时由后端校验兜底)
function optionsText(cfg: MetrologyScenario): string {
  return (cfg.required.options || []).map((o) => `${o.label}, ${o.description || ''}`).join('\n')
}
function setOptions(cfg: MetrologyScenario, ev: Event) {
  const text = (ev.target as HTMLTextAreaElement).value
  cfg.required.options = text.split('\n').filter((l) => l.trim()).map((line) => {
    const [label, description] = line.split(',').map((s) => s.trim())
    return { label: label || '', description: description || '' }
  })
}
function paramsText(cfg: MetrologyScenario): string {
  return JSON.stringify(cfg.action.params || {}, null, 0)
}
function setParams(cfg: MetrologyScenario, ev: Event) {
  try { cfg.action.params = JSON.parse((ev.target as HTMLTextAreaElement).value) }
  catch { /* 保留旧值,保存时后端校验兜底 */ }
}
function noticesText(cfg: MetrologyScenario): string {
  return JSON.stringify(cfg.action.notices || {}, null, 2)
}
function setNotices(cfg: MetrologyScenario, ev: Event) {
  try { cfg.action.notices = JSON.parse((ev.target as HTMLTextAreaElement).value) }
  catch { /* 保留旧值 */ }
}

onMounted(async () => {
  await reload()
  await loadHistory()
})
</script>

<style scoped>
.metro-admin { display: flex; flex-direction: column; gap: 14px; }
.metro-admin-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.metro-admin-title { font-size: 16px; font-weight: 600; }
.metro-admin-sub { color: #7a8491; font-size: 12px; margin-top: 4px; max-width: 640px; }
.metro-admin-foot { display: flex; align-items: center; gap: 12px; padding-top: 6px; border-top: 1px solid #edf0f3; }
.save-error { color: #d4380d; font-size: 12px; }
.metro-tab-body { display: flex; gap: 16px; min-height: 380px; }
.scenario-list { width: 220px; flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; }
.scenario-item { border: 1px solid #edf0f3; border-radius: 8px; padding: 10px 12px; cursor: pointer; transition: all .15s; }
.scenario-item:hover { border-color: #91caff; }
.scenario-item.active { border-color: #1677ff; background: #f0f7ff; }
.scenario-item-name { font-family: monospace; font-size: 12px; color: #1677ff; }
.scenario-item-label { font-size: 13px; margin-top: 2px; }
.scenario-editor { flex: 1; display: flex; flex-direction: column; gap: 12px; }
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #4a5568; }
.field input, .field textarea, .field select {
  border: 1px solid #d9dde3; border-radius: 6px; padding: 6px 10px; font-size: 13px;
}
.field input:disabled, .field textarea:disabled { background: #f5f6f8; color: #9aa4b2; }
.field textarea { resize: vertical; font-family: inherit; }
.slot-box { border: 1px solid #edf0f3; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
.slot-box legend { font-size: 12px; color: #7a8491; padding: 0 6px; }
.kg-hint { margin-top: 8px; }
.hist-tag { margin-left: 8px; }
</style>
