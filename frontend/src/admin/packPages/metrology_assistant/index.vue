<template>
  <!-- 计量客服管理页(自动路由版):
       Tab1 能力路由(实时总览:每个能力 自动/已润色/未接入 + 一键润色)
       Tab2 润色场景(可选的话术/规则覆盖;整档保存 revision CAS)
       Tab3 联调预演(草案 vs 当前对比)。
       路由本身全自动——本页只做可见性与可选润色,不做接入门槛。 -->
  <div class="metro-admin">
    <div class="metro-admin-head">
      <div>
        <div class="metro-admin-title">计量智能客服 · 能力路由</div>
        <div class="metro-admin-sub">
          <b>路由已自动化</b>:业务平台 MCP 注册的工具自动接入对话(每轮现算,上新接口零配置零发布);
          知识库/问数类问题由引擎自动分流。「润色」是可选项——为某个工具定制追问话术/状态通知/专用卡,
          不润色也照常路由执行。
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
      <!-- ── Tab1 能力路由:实时总览 ── -->
      <a-tab-pane key="routing">
        <template #tab><ApiOutlined /> 能力路由</template>
        <div class="routing">
          <div class="routing-bar">
            <span v-if="routingSummary" class="routing-summary">{{ routingSummary }}</span>
            <a-button size="small" :loading="routingLoading" @click="loadRouting">刷新</a-button>
          </div>
          <a-table
            :data-source="routing" :row-key="(r: any) => r.tool || r.scenario_key || r.label" size="small"
            :pagination="false" :loading="routingLoading"
          >
            <a-table-column title="能力" key="label">
              <template #default="{ record }">
                <div class="cap-title">
                  {{ record.label || record.tool }}
                  <a-tag v-if="record.kind === 'kg'" class="cap-kind">知识库</a-tag>
                </div>
                <div class="cap-desc">{{ record.description || '(无描述)' }}</div>
              </template>
            </a-table-column>
            <a-table-column title="路由" key="status" :width="110">
              <template #default="{ record }">
                <a-tag v-if="record.status === 'polished'" color="green">已润色</a-tag>
                <a-tag v-else-if="record.status === 'auto'" color="blue">自动</a-tag>
                <a-tooltip v-else :title="record.blocked_reason">
                  <a-tag color="red">未接入</a-tag>
                </a-tooltip>
              </template>
            </a-table-column>
            <a-table-column title="数据源/执行" key="ds" :width="200">
              <template #default="{ record }">
                <span v-if="record.kind === 'kg'">知识库证据检索</span>
                <span v-else-if="record.status === 'blocked'" class="cap-blocked">
                  {{ record.blocked_reason || '暂不可执行' }}
                </span>
                <span v-else-if="record.datasource?.datasource_name">
                  {{ record.datasource.datasource_name }}{{ record.status === 'auto' ? '(执行期解析)' : '' }}
                </span>
                <span v-else-if="record.datasource?.datasource_id" class="cap-mono">
                  {{ record.datasource.datasource_id.slice(0, 8) }}{{ record.status === 'auto' ? '(执行期解析)' : '' }}
                </span>
                <span v-else-if="record.datasource?.env" class="cap-mono">env:{{ record.datasource.env }}</span>
                <span v-else class="cap-dim">—</span>
              </template>
            </a-table-column>
            <a-table-column title="缺槽时追问" key="ask">
              <template #default="{ record }">
                <span v-if="record.ask?.question" :title="record.ask.question">
                  [{{ record.ask.header }}] {{ record.ask.question }}
                </span>
                <span v-else class="cap-dim">—</span>
              </template>
            </a-table-column>
            <a-table-column title="操作" key="ops" :width="170">
              <template #default="{ record }">
                <a-space size="small">
                  <a-button v-if="record.status === 'polished'" size="small"
                            @click="currentKey = record.scenario_key; tab = 'scenarios'">编辑</a-button>
                  <a-button v-if="record.status === 'auto' && record.kind === 'chatbi'" size="small"
                            type="primary" ghost :loading="polishing === record.tool"
                            @click="polishTool(record)">润色</a-button>
                  <a-button v-if="record.status !== 'blocked'" size="small"
                            @click="tryInDry(record)">试一句</a-button>
                </a-space>
              </template>
            </a-table-column>
          </a-table>
          <div v-if="routingError" class="cap-error">{{ routingError }}</div>
        </div>
      </a-tab-pane>

      <!-- ── Tab2 润色场景:可选覆盖的话术+规则 ── -->
      <a-tab-pane key="scenarios">
        <template #tab><ProfileOutlined /> 润色场景</template>
        <div class="metro-tab-body">
          <div class="scenario-list">
            <div class="scenario-list-head">
              <span>已润色({{ scenarioKeys.length }})</span>
              <a-button size="small" type="primary" ghost @click="addScenario">+ 新增</a-button>
            </div>
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
              <div class="field-grid">
                <label class="field">
                  <span>槽位类型(提取/补答按此校验归一)</span>
                  <select v-model="currentScenario.required.slot_type" @change="dirty = true">
                    <option value="text">text(自由文本)</option>
                    <option value="enum">enum(选项白名单)</option>
                    <option value="number">number(数字)</option>
                    <option value="date">date(日期 YYYY-MM-DD)</option>
                  </select>
                </label>
                <label v-if="currentScenario.required.slot_type === 'enum'" class="field">
                  <span>enum 白名单(每行一项;模糊唯一命中会归一到选项)</span>
                  <textarea :value="enumText(currentScenario)" rows="3"
                            @input="setEnum(currentScenario, $event); dirty = true" />
                </label>
              </div>
              <label class="field">
                <span>追问选项(label, description;每行一项)</span>
                <textarea
                  :value="optionsText(currentScenario)"
                  rows="4" @input="setOptions(currentScenario, $event); dirty = true"
                />
              </label>
            </fieldset>

            <fieldset class="slot-box">
              <legend>执行规则(数据源/工具/结果呈现)</legend>
              <div class="field-grid">
                <label class="field">
                  <span>动作类型</span>
                  <select v-model="currentScenario.action.kind" @change="dirty = true">
                    <option value="kg">kg(知识库证据检索)</option>
                    <option value="chatbi">chatbi(问数受控查询)</option>
                  </select>
                </label>
                <label v-if="currentScenario.action.kind === 'chatbi'" class="field">
                  <span>数据源(智能问数已登记的,选中即绑定)</span>
                  <select :value="currentScenario.action.datasource_id || ''"
                          @change="pickDatasource(currentScenario, $event); dirty = true">
                    <option value="">{{ dsBindingHint(currentScenario) }}</option>
                    <option v-for="d in datasources" :key="d.id" :value="d.id">
                      {{ d.name }}{{ dsOptionSuffix(d) }}
                    </option>
                  </select>
                </label>
              </div>
              <template v-if="currentScenario.action.kind === 'chatbi'">
                <div class="field-grid">
                  <label class="field">
                    <span>调用工具{{ serverTools(currentScenario).length ? '' : '(数据源 server 的工具名)' }}</span>
                    <select v-if="serverTools(currentScenario).length"
                            v-model="currentScenario.action.tool" @change="dirty = true">
                      <option v-for="t in serverTools(currentScenario)" :key="t" :value="t">{{ t }}</option>
                      <option v-if="currentScenario.action.tool && !serverTools(currentScenario).includes(currentScenario.action.tool)"
                              :value="currentScenario.action.tool">{{ currentScenario.action.tool }}(当前)</option>
                    </select>
                    <input v-else v-model="currentScenario.action.tool" placeholder="如 query_progress" @input="dirty = true" />
                  </label>
                  <label class="field">
                    <span>结果卡样式(通用表格卡免开发;专用卡需后端构建器)</span>
                    <input :value="currentScenario.action.card" disabled />
                  </label>
                </div>
                <label class="field">
                  <span>工具参数 ← 槽位映射(JSON:工具参数名 → 槽位 key,如 {"order_no": "order_no"})</span>
                  <textarea
                    :value="paramsText(currentScenario)"
                    rows="2" @input="setParams(currentScenario, $event); dirty = true"
                  />
                </label>
                <label class="field">
                  <span>业务状态通知文案(JSON:status → {title, message};常用键 not_found/forbidden/timeout)</span>
                  <textarea
                    :value="noticesText(currentScenario)"
                    rows="4" @input="setNotices(currentScenario, $event); dirty = true"
                  />
                </label>
              </template>
              <a-alert v-else type="info" show-icon>
                <template #message>kg 动作 = 知识库证据检索(强检判定):按用途自动选库 → 检索 → 三态结论。知识来源(用哪个库)在知识库侧维护,这里无需配数据源;要接业务查询/MCP 工具,切到 chatbi 并选数据源。</template>
              </a-alert>
            </fieldset>
          </div>
        </div>
      </a-tab-pane>

      <!-- ── Tab3 联调预演 ── -->
      <a-tab-pane key="dryrun">
        <template #tab><ExperimentOutlined /> 联调预演</template>
        <div class="dryrun">
          <div class="dryrun-bar">
            <input v-model="dry.text" class="dryrun-input" placeholder="输入一句话,如: 查一下 BJ2026 的进度" />
            <label class="dryrun-check"><input type="checkbox" v-model="dry.execute" /> 真实执行</label>
            <a-button type="primary" size="small" :loading="dry.loading" @click="runDry('current')">运行预演</a-button>
            <a-button size="small" :disabled="!dirty" @click="runDry('diff')">对比草案与当前</a-button>
          </div>
          <div v-if="dry.current" class="dryrun-grid">
            <div class="dryrun-col">
              <div class="dryrun-col-title">{{ dry.diff ? '当前生效配置' : '预演结果' }}</div>
              <pre class="dryrun-pre">{{ fmtDry(dry.current) }}</pre>
            </div>
            <div v-if="dry.diff" class="dryrun-col">
              <div class="dryrun-col-title">未保存的草案</div>
              <pre class="dryrun-pre" :class="{ changed: fmtDry(dry.draft) !== fmtDry(dry.current) }">{{ fmtDry(dry.draft) }}</pre>
            </div>
          </div>
          <div v-if="dry.error" class="dryrun-error">{{ dry.error }}</div>
          <div class="dryrun-note">
            预演只读(提取+求值,展示命中场景/已识别槽位/缺口/将追问的原文/将调用的数据源与工具);
            勾选「真实执行」且槽位齐时对问数受控执行跑一次真实调用。预演永不写对话/任务。
            与「智能客服-测试」菜单的分工:测试菜单验证已生效配置;本页预演面向<b>未保存的草案</b>(可左右对比当前),是保存前最后一道验证。
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
// 计量客服管理页(自动路由版) —— 三 tab 编排壳:
//   能力路由(/config/routing 实时总览) + 润色场景(话术+规则合一编辑) + 联调预演。
// 保存语义:整档提交 + revision 乐观锁(409 提示刷新)+ 历史留痕 + 回滚。
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ApiOutlined, ExperimentOutlined, ProfileOutlined } from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'
import { metrologyApi, type MetrologyConfig, type MetrologyHistoryItem, type MetrologyScenario } from '../../metrologyApi'

const tab = ref('routing')
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

// ── Tab1 能力路由(/config/routing:自动/已润色/未接入) ──
interface RoutingCap {
  kind: 'chatbi' | 'kg'
  tool: string | null
  server: string | null
  description: string
  status: 'auto' | 'polished' | 'blocked'
  blocked_reason?: string
  scenario_key: string | null
  label?: string
  datasource?: { datasource_id: string; env: string; server: string } | null
  ask?: { slot: string; header: string; question: string } | null
}
const routing = ref<RoutingCap[]>([])
const routingLoading = ref(false)
const routingError = ref('')
const polishing = ref('')

const routingSummary = computed(() => {
  if (!routing.value.length) return ''
  const auto = routing.value.filter((r) => r.status === 'auto').length
  const polished = routing.value.filter((r) => r.status === 'polished').length
  const blocked = routing.value.filter((r) => r.status === 'blocked').length
  const parts = [`${routing.value.length} 个能力`]
  if (auto) parts.push(`${auto} 自动`)
  if (polished) parts.push(`${polished} 已润色`)
  if (blocked) parts.push(`${blocked} 未接入`)
  return parts.join(' · ')
})

async function loadRouting() {
  routingLoading.value = true
  routingError.value = ''
  try {
    const { data } = await metrologyApi.get('/config/routing')
    routing.value = data.capabilities || []
  } catch (e: any) {
    routingError.value = e?.response?.data?.detail || '路由总览读取失败'
  } finally {
    routingLoading.value = false
  }
}

// 润色 = 从工具的自动配置起草(不是空白模板),落进编辑器改话术后整档保存
async function polishTool(row: RoutingCap) {
  polishing.value = row.tool || ''
  routingError.value = ''
  try {
    const { data } = await metrologyApi.post('/config/capability-import/draft',
      { server: row.server, tool: row.tool })
    let key = data.key
    let n = 2
    while (draft.value.scenarios[key]) key = `${data.key}_${n++}`
    draft.value.scenarios[key] = data.scenario
    currentKey.value = key
    tab.value = 'scenarios'
    dirty.value = true
  } catch (e: any) {
    routingError.value = e?.response?.data?.detail || '润色起草失败'
  } finally {
    polishing.value = ''
  }
}

function tryInDry(row: RoutingCap) {
  // 不做猜测式预填(追问文案里的示例截出来往往不成句)——切到预演
  // 让用户自己输入,placeholder 已有示例
  dry.text = ''
  dry.error = ''
  tab.value = 'dryrun'
}

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
    await loadRouting()
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
    await loadRouting()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '回滚失败')
  }
}

// 选项/参数/通知:JSON 文本 ↔ 结构(表单友好;非法 JSON 保存时由后端校验兜底)
function enumText(cfg: MetrologyScenario): string {
  return (cfg.required as any).enum_options?.join('\n') || ''
}
function setEnum(cfg: MetrologyScenario, ev: Event) {
  const text = (ev.target as HTMLTextAreaElement).value
  ;(cfg.required as any).enum_options = text.split('\n').map((l) => l.trim()).filter(Boolean)
}

// ── ChatBI 数据源下拉(动作绑定写显式 datasource_id;env 只是出厂缺省) ──
interface ChatbiDs { id: string; name: string; dbType: string; host: string; database?: string }
const datasources = ref<ChatbiDs[]>([])

async function loadDatasources() {
  try {
    const { data } = await chatbiApi.get('/datasources')
    datasources.value = data.items || []
  } catch { /* 拉不到时下拉退化为提示文案;保存校验兜底 */ }
}

function dsOptionSuffix(d: ChatbiDs): string {
  return d.dbType === 'mcp' ? `（${d.host}${d.database ? ':' + d.database : ''}）` : ''
}

function dsBindingHint(cfg: MetrologyScenario): string {
  if (cfg.action.datasource_id) return ''
  return cfg.action.env
    ? `未显式绑定——缺省走环境变量 ${cfg.action.env}(建议改为显式绑定)`
    : '未绑定(保存前需选择一个)'
}

function pickDatasource(cfg: MetrologyScenario, ev: Event) {
  const id = (ev.target as HTMLSelectElement).value
  const d = datasources.value.find((x) => x.id === id)
  cfg.action.datasource_id = id
  if (d) {
    cfg.action.ds_name = d.name
    // mcp 数据源建源时限定过工具(database 字段)——带出为默认工具
    if (d.dbType === 'mcp' && d.database && !cfg.action.tool) cfg.action.tool = d.database
  }
}

function serverTools(cfg: MetrologyScenario): string[] {
  // 绑定 mcp 数据源时,用能力路由表(server → tools)把工具变下拉
  const d = datasources.value.find((x) => x.id === cfg.action.datasource_id)
  if (!d || d.dbType !== 'mcp') return []
  const names = routing.value
    .filter((r) => r.kind === 'chatbi' && r.server === d.host)
    .map((r) => r.tool || '')
    .filter(Boolean)
  return names
}

// 手工新增润色场景:chatbi 模板(通用表格卡);工具/参数选好前保存会被校验拦下
function addScenario() {
  let key = 'custom_1'
  let n = 2
  while (draft.value.scenarios[key]) key = `custom_${n++}`
  draft.value.scenarios[key] = {
    label: '新场景',
    intent: '描述这类问题的典型问法(供意图识别)',
    required: {
      slot: 'keyword', header: '关键词', question: '请提供要查的关键词:',
      options: [], slot_type: 'text',
    },
    action: {
      kind: 'chatbi', datasource_id: '', env: '', ds_name: '',
      tool: '', params: {}, stage: '查询中…', error_noun: '业务',
      notices: {}, card: '_table_card',
    },
  }
  currentKey.value = key
  tab.value = 'scenarios'
  dirty.value = true
}

// ── Tab3 联调预演:一句话走提取+求值;草案差异对比 ──
const dry = reactive({
  text: '', execute: false, loading: false,
  current: null as any, draft: null as any, diff: false, error: '',
})

function fmtDry(r: any): string {
  if (!r) return ''
  const lines: string[] = [`输入: ${r.input}`, `命中场景: ${r.scenario ?? '—'}`]
  if (r.slots && Object.keys(r.slots).length)
    lines.push(`已识别槽位: ${Object.entries(r.slots).map(([k, v]) => `${k}=${v}`).join(' ')}`)
  if (r.will_ask) lines.push(`将追问(${r.will_ask.header}): ${r.will_ask.question}`)
  if (r.action) {
    const a = r.action
    lines.push(a.kind === 'kg'
      ? `将调用: 知识库证据检索 → 「${a.kb}」`
      : a.auto
        ? `将调用: ${a.server}:${a.tool}(自动路由,执行期解析数据源)(${JSON.stringify(a.params)})`
        : `将调用: ${a.datasource_name} · ${a.tool}(${JSON.stringify(a.params)})`)
  }
  if (r.note) lines.push(`说明: ${r.note}`)
  if (r.executed) lines.push(r.executed.ok
    ? `真实执行: ok(${r.executed.rows ?? '?'} 行)`
    : `真实执行: 失败(${r.executed.error || r.executed.status})`)
  return lines.join('\n')
}

async function runDry(mode: 'current' | 'diff') {
  if (!dry.text.trim()) { dry.error = '请输入一句话'; return }
  dry.loading = true
  dry.error = ''
  dry.diff = mode === 'diff'
  try {
    const base = { text: dry.text, execute: dry.execute }
    const { data } = await metrologyApi.post('/config/dry-run',
      mode === 'diff' ? { ...base, config: { scenarios: draft.value.scenarios } } : base)
    dry.current = data
    if (mode === 'diff') {
      const { data: d } = await metrologyApi.post('/config/dry-run', base)
      dry.draft = dry.current
      dry.current = d  // 左=当前,右=草案
    }
  } catch (e: any) {
    dry.error = e?.response?.data?.detail || e?.message || '预演失败'
  } finally {
    dry.loading = false
  }
}

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
  loadRouting()
  loadDatasources()
})
</script>

<style scoped>
.metro-admin { display: flex; flex-direction: column; gap: 14px; }
.metro-admin-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.metro-admin-title { font-size: 16px; font-weight: 600; }
.metro-admin-sub { color: #7a8491; font-size: 12px; margin-top: 4px; max-width: 640px; }
.metro-admin-foot { display: flex; align-items: center; gap: 12px; padding-top: 6px; border-top: 1px solid #edf0f3; }
.save-error { color: #d4380d; font-size: 12px; }
.routing { display: flex; flex-direction: column; gap: 10px; }
.routing-bar { display: flex; align-items: center; gap: 12px; }
.routing-summary { color: #4a5568; font-size: 12px; }
.cap-title { font-size: 13px; font-weight: 500; }
.cap-kind { margin-left: 6px; }
.cap-desc { color: #9aa4b2; font-size: 12px; margin-top: 2px; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cap-mono { font-family: monospace; font-size: 12px; }
.cap-dim { color: #c0c6cf; }
.cap-blocked { color: #d4380d; font-size: 12px; }
.cap-error { color: #d4380d; font-size: 12px; }
.metro-tab-body { display: flex; gap: 16px; min-height: 380px; }
.scenario-list { width: 220px; flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; }
.scenario-list-head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #7a8491; padding: 0 2px; }
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
.hist-tag { margin-left: 8px; }
.dryrun { display: flex; flex-direction: column; gap: 12px; }
.dryrun-bar { display: flex; align-items: center; gap: 12px; }
.dryrun-input { flex: 1; max-width: 460px; border: 1px solid #d9dde3; border-radius: 6px; padding: 7px 10px; font-size: 13px; }
.dryrun-check { font-size: 12px; color: #4a5568; display: flex; align-items: center; gap: 4px; }
.dryrun-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.dryrun-col-title { font-size: 12px; color: #7a8491; margin-bottom: 6px; }
.dryrun-pre { background: #f6f7f9; border: 1px solid #edf0f3; border-radius: 8px; padding: 12px; font-size: 12px; line-height: 1.9; white-space: pre-wrap; }
.dryrun-pre.changed { border-color: #faad14; background: #fffbe6; }
.dryrun-error { color: #d4380d; font-size: 12px; }
.dryrun-note { color: #9aa4b2; font-size: 12px; line-height: 1.8; }
</style>
