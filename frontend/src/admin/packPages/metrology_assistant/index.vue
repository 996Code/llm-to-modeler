<template>
  <!-- 智能客服管理页(自动路由版):
       Tab1 能力路由(实时总览:每个能力 自动/已润色/未接入 + 一键润色)
       Tab2 润色场景(可选的话术/规则覆盖;整档直存)
       Tab3 联调预演(草案 vs 当前对比)。
       路由本身全自动——本页只做可见性与可选润色,不做接入门槛。 -->
  <div class="metro-admin">
    <a-tabs v-model:activeKey="tab" class="flat-tabs">
      <template #rightExtra>
        <div class="metro-tab-actions">
          <a-button v-if="tab === 'routing'" size="small" type="text" :loading="routingLoading" @click="loadRouting"><ReloadOutlined /> 刷新</a-button>
          <a-popover :key="tab" trigger="click" placement="bottomRight">
            <template #content>
              <div class="help-pop help-pop-wide" v-html="currentHelpHtml"></div>
            </template>
            <a-button type="text" size="small" aria-label="智能客服说明" class="help-link"><QuestionCircleOutlined /> 说明</a-button>
          </a-popover>
        </div>
      </template>
      <!-- ── Tab1 能力路由:实时总览 ── -->
      <a-tab-pane key="routing">
        <template #tab>
          <ApiOutlined /> 能力路由
          <a-tooltip :title="routingSummary"><span class="metro-tab-count">{{ routing.length }}</span></a-tooltip>
        </template>
        <div class="routing">
          <!-- 服务画像头:按 MCP 服务分组,声明质量一目了然(缺服务说明/
               声明不完整都标红,提示找业务平台补声明而非进润色场景改) -->
          <div v-for="grp in serverGroups" :key="grp.code" class="srv-group">
            <div class="srv-head">
              <span class="srv-name">{{ grp.name }}</span>
              <span class="srv-desc">{{ grp.description || '未填服务说明' }}</span>
              <span class="srv-stats">
                {{ grp.caps.length }} 项能力
                <template v-if="grp.incomplete"> · <a-tag color="red">{{ grp.incomplete }} 声明不完整</a-tag></template>
                <template v-if="grp.blocked"> · <a-tag color="orange">{{ grp.blocked }} 未接入</a-tag></template>
              </span>
            </div>
            <a-table
              :data-source="grp.caps" :row-key="(r: any) => r.tool || r.scenario_key || r.label" size="small"
              :pagination="false" :loading="routingLoading" :scroll="{ x: 960 }"
            >
              <a-table-column title="能力" key="label">
                <template #default="{ record }">
                  <div class="cap-title">
                    {{ record.label || record.tool }}
                    <a-tag v-if="record.kind === 'kg'" class="cap-kind">知识库</a-tag>
                  </div>
                  <div class="cap-desc" :title="record.description">{{ record.description || '暂无描述' }}</div>
                </template>
              </a-table-column>
              <a-table-column title="路由" key="status" :width="110">
                <template #default="{ record }">
                  <a-tag v-if="record.status === 'polished'" color="green">已润色</a-tag>
                  <a-tag v-else-if="record.status === 'auto'" color="blue">自动</a-tag>
                  <a-tooltip v-else-if="record.status === 'incomplete'"
                             :title="(record.issues || ['声明不完整']).join('；') + '——请业务平台补充 MCP 声明'">
                    <a-tag color="red">声明不完整</a-tag>
                  </a-tooltip>
                  <a-tooltip v-else :title="record.blocked_reason">
                    <a-tag color="orange">未接入</a-tag>
                  </a-tooltip>
                </template>
              </a-table-column>
              <a-table-column title="数据源" key="ds" :width="200">
                <template #default="{ record }">
                  <span v-if="record.kind === 'kg'">知识库证据检索</span>
                  <span v-else-if="record.status === 'incomplete'" class="cap-blocked">
                    {{ (record.issues || []).join('；') || '声明不完整' }}
                  </span>
                  <span v-else-if="record.status === 'blocked'" class="cap-blocked">
                    {{ record.blocked_reason || '暂不可执行' }}
                  </span>
                  <span v-else-if="record.datasource?.datasource_name">
                    {{ record.datasource.datasource_name }}
                  </span>
                  <span v-else-if="record.datasource?.datasource_id" class="cap-mono">
                    {{ record.datasource.datasource_id.slice(0, 8) }}
                  </span>
                  <span v-else-if="record.datasource?.env" class="cap-dim">默认数据源</span>
                  <span v-else class="cap-dim">—</span>
                </template>
              </a-table-column>
              <a-table-column title="信息不足时追问" key="ask">
                <template #default="{ record }">
                  <span v-if="record.ask?.question" :title="record.ask.question">
                    [{{ record.ask.header }}] {{ record.ask.question }}
                  </span>
                  <span v-else class="cap-dim">—</span>
                </template>
              </a-table-column>
              <a-table-column title="操作" key="ops" :width="200">
                <template #default="{ record }">
                  <a-space size="small" wrap>
                    <a-tooltip v-if="record.status === 'polished'" title="编辑覆盖">
                      <a-button size="small" type="text" aria-label="编辑覆盖" @click="openCapEditor(record)"><EditOutlined /></a-button>
                    </a-tooltip>
                    <a-tooltip v-if="record.status === 'auto' && record.kind === 'chatbi'" title="以此声明为起点创建覆盖">
                      <a-button size="small" type="text" aria-label="创建覆盖" :loading="polishing === record.tool" @click="openCapEditor(record)"><FormOutlined /></a-button>
                    </a-tooltip>
                    <a-tooltip v-if="record.status === 'auto' || record.status === 'polished'" title="预演能力">
                      <a-button size="small" type="text" aria-label="预演能力" @click="tryInDry(record)"><ExperimentOutlined /></a-button>
                    </a-tooltip>
                    <!-- 行级重置:放弃本能力的覆盖,回到按 MCP 最新声明自动生成
                         (全局刷新不动任何覆盖;这个按钮是唯一的"以声明为准"出口) -->
                    <a-tooltip v-if="record.status === 'polished' && record.kind === 'chatbi'" title="重置为最新声明(放弃覆盖)">
                      <a-popconfirm title="放弃此能力的覆盖,回到 MCP 最新声明的自动生成?"
                                    ok-text="重置" ok-type="danger" cancel-text="取消" @confirm="resetToLatest(record)">
                        <a-button size="small" type="text" class="cap-reset-btn" aria-label="重置为最新声明"
                                  :loading="resetting === record.scenario_key">
                          <RollbackOutlined />
                        </a-button>
                      </a-popconfirm>
                    </a-tooltip>
                  </a-space>
                </template>
              </a-table-column>
            </a-table>
          </div>
          <div v-if="!serverGroups.length && !routingLoading" class="cap-dim">暂无已注册的 MCP 服务</div>
          <div v-if="routingError" class="cap-error">{{ routingError }}</div>
        </div>
      </a-tab-pane>

      <!-- ── Tab2 联调预演 ── -->
      <a-tab-pane key="dryrun">
        <template #tab><ExperimentOutlined /> 联调预演</template>
        <div class="dryrun">
          <div class="dryrun-bar">
            <input v-model="dry.text" class="dryrun-input" placeholder="输入一句话,如: 查一下 BJ2026 的进度" />
            <label class="dryrun-check"><input type="checkbox" v-model="dry.execute" /> 真实执行</label>
            <a-button type="primary" size="small" :loading="dry.loading" @click="runDry()"><PlayCircleOutlined /> 运行预演</a-button>
          </div>
          <div v-if="dry.current" class="dryrun-grid">
            <div class="dryrun-col">
              <div class="dryrun-col-title">预演结果</div>
              <pre class="dryrun-pre">{{ fmtDry(dry.current) }}</pre>
            </div>
          </div>
          <div v-if="dry.error" class="dryrun-error">{{ dry.error }}</div>
        </div>
      </a-tab-pane>
    </a-tabs>

    <!-- 能力编辑抽屉:声明参考(最新快照,只读) + 覆盖编辑(以本页为准);
         保存=整档落库;行级「重置为最新声明」放弃覆盖回到自动生成 -->
    <a-drawer v-model:open="editorOpen" width="min(720px, 92vw)"
              :title="editorTitle" class="cap-editor-drawer">
      <div v-if="editorCap" class="ed-decl">
        <div class="ed-decl-title">MCP 声明参考(只读,刷新能力路由取最新)</div>
        <div class="ed-decl-body">{{ editorCap.description || '(无描述)' }}</div>
        <div v-if="editorCap.issues?.length" class="ed-decl-issues">声明问题:{{ editorCap.issues.join('；') }}</div>
      </div>
      <div v-if="currentScenario" class="scenario-editor">
            <div class="field-grid">
              <label class="field">
                <span>场景标识</span>
                <input :value="currentKey" disabled />
              </label>
              <label class="field">
                <span>显示名</span>
                <input v-model="currentScenario.label" />
              </label>
            </div>
            <label class="field">
              <span>意图描述</span>
              <textarea v-model="currentScenario.intent" rows="2" />
            </label>
            <fieldset class="slot-box">
              <legend>必填信息与追问</legend>
              <div class="field-grid">
                <label class="field">
                  <span>信息标识</span>
                  <input :value="currentScenario.required.slot" disabled />
                </label>
                <label class="field">
                  <span>追问标题(最多12字)</span>
                  <input v-model="currentScenario.required.header" />
                </label>
              </div>
              <label class="field">
                <span>追问文案({subject}=器具名占位)</span>
                <textarea v-model="currentScenario.required.question" rows="2" />
              </label>
              <div class="field-grid">
                <label class="field">
                  <span>信息类型</span>
                  <select v-model="currentScenario.required.slot_type">
                    <option value="text">text(自由文本)</option>
                    <option value="enum">enum(选项白名单)</option>
                    <option value="number">number(数字)</option>
                    <option value="date">date(日期 YYYY-MM-DD)</option>
                  </select>
                </label>
                <label v-if="currentScenario.required.slot_type === 'enum'" class="field">
                  <span>允许的选项(每行一项)</span>
                  <textarea :value="enumText(currentScenario)" rows="3"
                            @input="setEnum(currentScenario, $event)" />
                </label>
              </div>
              <label class="field">
                <span>追问选项 <a-tooltip title="每行填写一个选项，格式为：名称, 说明"><QuestionCircleOutlined /></a-tooltip></span>
                <textarea
                  :value="optionsText(currentScenario)"
                  rows="4" @input="setOptions(currentScenario, $event)"
                />
              </label>
            </fieldset>

            <fieldset class="slot-box">
              <legend>执行规则</legend>
              <div class="field-grid">
                <label class="field">
                  <span>动作类型</span>
                  <select v-model="currentScenario.action.kind">
                    <option value="kg">知识库检索</option>
                    <option value="chatbi">业务查询</option>
                  </select>
                </label>
                <label v-if="currentScenario.action.kind === 'chatbi'" class="field">
                  <span>数据源</span>
                  <select :value="currentScenario.action.datasource_id || ''"
                          @change="pickDatasource(currentScenario, $event)">
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
                    <span>调用工具</span>
                    <select v-if="serverTools(currentScenario).length"
                            v-model="currentScenario.action.tool">
                      <option v-for="t in serverTools(currentScenario)" :key="t" :value="t">{{ t }}</option>
                      <option v-if="currentScenario.action.tool && !serverTools(currentScenario).includes(currentScenario.action.tool)"
                              :value="currentScenario.action.tool">{{ currentScenario.action.tool }}(当前)</option>
                    </select>
                    <input v-else v-model="currentScenario.action.tool" placeholder="如 query_progress" />
                  </label>
                  <label class="field">
                    <span>结果卡样式</span>
                    <input :value="currentScenario.action.card" disabled />
                  </label>
                </div>
                <label class="field">
                  <span>工具参数映射 <a-tooltip title='填写 JSON，键为工具参数名，值为信息标识。例如 {"order_no": "order_no"}'><QuestionCircleOutlined /></a-tooltip></span>
                  <textarea
                    :value="paramsText(currentScenario)"
                    rows="2" @input="setParams(currentScenario, $event)"
                  />
                </label>
                <label class="field">
                  <span>状态通知 <a-tooltip title="填写 JSON，按状态配置 title 和 message。常用状态为 not_found、forbidden、timeout。"><QuestionCircleOutlined /></a-tooltip></span>
                  <textarea
                    :value="noticesText(currentScenario)"
                    rows="4" @input="setNotices(currentScenario, $event)"
                  />
                </label>
              </template>
              <a-tooltip v-else title="知识来源在知识库管理中维护；调用业务工具请选择业务查询并绑定数据源。">
                <span class="field-help"><QuestionCircleOutlined /> 知识库检索设置</span>
              </a-tooltip>
            </fieldset>
          </div>
      <div v-if="saveError" class="save-error">{{ saveError }}</div>
      <template #footer>
        <a-space wrap>
          <a-button type="primary" :loading="saving" @click="drawerSave"><SaveOutlined /> 保存</a-button>
          <a-button @click="cancelEdit">取消</a-button>
        </a-space>
        <div class="ed-foot-note">覆盖保存后以本页为准;要回到 MCP 最新声明,请在能力列表行内点「重置为最新声明」</div>
      </template>
    </a-drawer>

  </div>
</template>

<script setup lang="ts">
// 智能客服管理页 —— 能力路由(总览+行内编辑抽屉) + 联调预演。
// 编辑语义:抽屉内改覆盖、保存即整档落库(以本页为准,优先于 MCP 声明);
// 行级「重置为最新声明」=放弃覆盖回到自动;全局刷新只重拉声明、永不动覆盖。
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ApiOutlined, DiffOutlined, EditOutlined, ExperimentOutlined, FormOutlined, PlayCircleOutlined, PlusOutlined, ProfileOutlined, QuestionCircleOutlined, ReloadOutlined, RollbackOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'
import { chip, iconBtn, rules } from '../../helpHtml'
import { metrologyApi, type MetrologyConfig, type MetrologyScenario } from '../../metrologyApi'

const tab = ref('routing')
// 说明逐条列出(规则一条条讲清楚,状态词彩色标签,按钮引用同款图标)——构造器共享自 helpHtml.ts,
// 样式走 admin.css 的全局 .help-pop(popover 门户在组件树外,scoped 命中不了)。
// chip 颜色与能力表状态视觉一致:蓝=自动 绿=已覆盖 红=声明不完整 橙=未接入
const helpHtmlMap: Record<string, string> = {
  routing: rules([
    `声明完整的 MCP 工具自动接入对话,零配置可用(${chip('blue', '自动')})。`,
    `<b>刷新</b> = 重新获取全部 MCP 最新声明:${chip('green', '已覆盖')}的能力<b>不受影响</b>(始终以你的覆盖为准);未覆盖的能力按最新声明自动更新。`,
    `${iconBtn('edit', '编辑覆盖')} / ${iconBtn('form', '创建覆盖')} 可定制该能力的追问文案、通知等,保存后以此为准;MCP 声明原文在编辑抽屉顶部供参考。`,
    `行内 ${iconBtn('rollback', '重置为最新声明', 'orange')} = 放弃该能力的覆盖,回到按 MCP 声明自动生成——唯一"以声明为准"的出口。`,
    `${iconBtn('experiment', '预演能力')} 跳到联调预演,直接试这句能力(默认只看计划,勾选「真实执行」才真调工具)。`,
    `${chip('red', '声明不完整')} = 工具描述缺失,不进入对话路由;请业务平台补充 MCP 声明后点「刷新」,补全即自动接入。`,
    `${chip('orange', '未接入')} = 该服务还没有绑定的数据源;在智能问数的数据源页接入后点「刷新」即可。`,
  ]),
  dryrun: rules([
    '输入一句话验证路由:展示命中的能力、识别到的信息、缺什么会追问什么。',
    '勾选<b>「真实执行」</b>后,信息齐全时会真实调用业务工具并返回数据。',
    '预演按当前生效配置运行(覆盖优先于声明),不写入对话与任务。',
  ]),
}
const currentHelpHtml = computed(() => helpHtmlMap[tab.value] || helpHtmlMap.routing)
const config = ref<MetrologyConfig>({ revision: 0, factory: true, content: { scenarios: {} } })
const draft = ref<{ scenarios: Record<string, MetrologyScenario> }>({ scenarios: {} })
const saving = ref(false)
const saveError = ref('')

// ── 能力编辑抽屉(行内入口;编辑 draft 副本,保存才落库) ──
const editorOpen = ref(false)
const editorCap = ref<RoutingCap | null>(null)
const currentKey = ref('')
const editSnapshot = ref<MetrologyScenario | null>(null)   // 打开时的快照(取消还原)
const currentScenario = computed(() => draft.value.scenarios[currentKey.value])
const editorTitle = computed(() => editorCap.value
  ? `编辑「${editorCap.value.label || editorCap.value.tool}」`
  : '编辑能力')

async function openCapEditor(cap: RoutingCap) {
  saveError.value = ''
  editorCap.value = cap
  if (cap.status === 'polished' && cap.scenario_key) {
    currentKey.value = cap.scenario_key
  } else if (cap.status === 'auto' && cap.kind === 'chatbi') {
    // 自动能力:先按声明起草一份覆盖(落到 draft,未保存不生效)
    polishing.value = cap.tool || ''
    try {
      const { data } = await metrologyApi.post('/config/capability-import/draft',
        { server: cap.server, tool: cap.tool })
      let key = data.key
      let n = 2
      while (draft.value.scenarios[key]) key = `${data.key}_${n++}`
      draft.value.scenarios[key] = data.scenario
      currentKey.value = key
    } catch (e: any) {
      routingError.value = e?.response?.data?.detail || '起草失败'
      polishing.value = ''
      return
    }
    polishing.value = ''
  } else {
    return
  }
  editSnapshot.value = JSON.parse(JSON.stringify(draft.value.scenarios[currentKey.value] || null))
  editorOpen.value = true
}

function cancelEdit() {
  if (editSnapshot.value && currentKey.value) {
    draft.value.scenarios[currentKey.value] = editSnapshot.value
  }
  editorOpen.value = false
  saveError.value = ''
}

async function drawerSave() {
  saving.value = true
  saveError.value = ''
  try {
    await metrologyApi.put('/config', {
      content: draft.value,
      comment: `能力路由编辑(${Object.keys(draft.value.scenarios).length} 个场景)`,
    })
    message.success('已保存,下一次对话生效')
    editorOpen.value = false
    await reload()
    await loadRouting()
  } catch (e: any) {
    if (e?.response?.status === 400) saveError.value = `校验失败: ${e.response.data?.detail || ''}`
    else saveError.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

// 行级重置:放弃该能力的覆盖,回到按 MCP 最新声明自动生成
const resetting = ref('')
async function resetToLatest(cap: RoutingCap) {
  if (!cap.scenario_key) return
  resetting.value = cap.scenario_key
  try {
    delete draft.value.scenarios[cap.scenario_key]
    await metrologyApi.put('/config', {
      content: draft.value,
      comment: `重置为最新声明 ${cap.scenario_key}(放弃覆盖)`,
    })
    message.success('已重置,按 MCP 最新声明自动生成')
    await reload()
    await loadRouting()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '重置失败')
    await reload()
  } finally {
    resetting.value = ''
  }
}

// ── Tab1 能力路由(/config/routing:自动/已润色/未接入/声明不完整) ──
interface RoutingCap {
  kind: 'chatbi' | 'kg'
  tool: string | null
  server: string | null
  description: string
  status: 'auto' | 'polished' | 'blocked' | 'incomplete'
  blocked_reason?: string
  issues?: string[] | null
  scenario_key: string | null
  label?: string
  datasource?: { datasource_id: string; datasource_name?: string; env: string; server: string } | null
  ask?: { slot: string; header: string; question: string } | null
}
interface ServerProfile {
  code: string
  name: string
  description: string
  tools: number
  incomplete: number
  warnings: number
  enabled: boolean
  status: string
  issues?: string[]
}
const routing = ref<RoutingCap[]>([])
const serverProfiles = ref<ServerProfile[]>([])
const routingLoading = ref(false)
const routingError = ref('')
const polishing = ref('')

// 分组视图:每个 MCP 服务一个小节(画像头 + 能力表);kg 能力归"知识库"组
const serverGroups = computed(() => {
  const groups: { code: string; name: string; description: string;
                  caps: RoutingCap[]; incomplete: number; blocked: number }[] = []
  const byCode = new Map<string, typeof groups[number]>()
  for (const p of serverProfiles.value) {
    const g = { code: p.code, name: p.name || p.code,
                description: p.description || '', caps: [],
                incomplete: 0, blocked: 0 }
    byCode.set(p.code, g)
    groups.push(g)
  }
  const kgGroup = { code: '__kg', name: '知识库', description: '内置知识图谱检索',
                    caps: [] as RoutingCap[], incomplete: 0, blocked: 0 }
  for (const cap of routing.value) {
    const g = cap.server ? byCode.get(cap.server) : undefined
    if (g) {
      g.caps.push(cap)
      if (cap.status === 'incomplete') g.incomplete++
      if (cap.status === 'blocked') g.blocked++
    } else {
      kgGroup.caps.push(cap)
    }
  }
  if (kgGroup.caps.length) groups.push(kgGroup)
  return groups.filter((g) => g.caps.length || serverProfiles.value.some((p) => p.code === g.code))
})

const routingSummary = computed(() => {
  if (!routing.value.length) return ''
  const auto = routing.value.filter((r) => r.status === 'auto').length
  const polished = routing.value.filter((r) => r.status === 'polished').length
  const blocked = routing.value.filter((r) => r.status === 'blocked').length
  const incomplete = routing.value.filter((r) => r.status === 'incomplete').length
  const parts = [`${routing.value.length} 个能力`]
  if (auto) parts.push(`${auto} 自动`)
  if (polished) parts.push(`${polished} 已润色`)
  if (blocked) parts.push(`${blocked} 未接入`)
  if (incomplete) parts.push(`${incomplete} 声明不完整`)
  return parts.join(' · ')
})

async function loadRouting() {
  routingLoading.value = true
  routingError.value = ''
  try {
    const { data } = await metrologyApi.get('/config/routing')
    routing.value = data.capabilities || []
    serverProfiles.value = data.servers || []
  } catch (e: any) {
    routingError.value = e?.response?.data?.detail || '路由总览读取失败'
  } finally {
    routingLoading.value = false
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
  saveError.value = ''
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
    ? '使用默认数据源'
    : '请选择数据源'
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

// ── Tab3 联调预演:一句话走提取+求值;草案差异对比 ──
const dry = reactive({
  text: '', execute: false, loading: false,
  current: null as any, error: '',
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

async function runDry() {
  if (!dry.text.trim()) { dry.error = '请输入一句话'; return }
  dry.loading = true
  dry.error = ''
  try {
    const { data } = await metrologyApi.post('/config/dry-run',
      { text: dry.text, execute: dry.execute })
    dry.current = data
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
  loadRouting()
  loadDatasources()
})
</script>

<style scoped>
.metro-admin { display: flex; flex-direction: column; gap: 16px; min-width: 0; font-size: 13px; container-type: inline-size; }
.metro-admin :deep(.flat-tabs > .ant-tabs-nav) { margin-bottom: var(--admin-header-gap, 12px); }
.metro-admin :deep(.ant-tabs-tab), .metro-admin :deep(.ant-table) { font-size: 13px; }
/* 说明弹窗样式与构造器已共享:admin.css 的 .help-pop + src/admin/helpHtml.ts */
.save-error { color: #d4380d; font-size: 12px; overflow-wrap: anywhere; }
.metro-tab-count { display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 20px; margin-left: 6px; padding: 0 6px; border-radius: 4px; background: #f1f5f9; color: #64748b; font-size: 12px; font-weight: 400; }
.routing { display: flex; flex-direction: column; gap: 16px; }
.metro-tab-actions { display: flex; align-items: center; gap: 4px; }
.srv-group { display: flex; flex-direction: column; gap: 8px; }
.srv-group + .srv-group { margin-top: 16px; }
.srv-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; padding: 0 2px; }
.srv-name { font-size: 13px; font-weight: 600; color: #1f2937; }
.srv-desc { flex: 1; min-width: 0; font-size: 12px; color: #86909c; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.srv-stats { font-size: 12px; color: #6b7280; display: inline-flex; align-items: center; gap: 4px; }
.cap-title { font-size: 13px; font-weight: 500; }
.cap-kind { margin-left: 6px; }
.cap-desc { color: #7a8491; font-size: 12px; margin-top: 2px; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cap-mono { font-family: monospace; font-size: 12px; }
.cap-dim { color: #86909c; }
.cap-reset-btn { color: #d46b08; }
.cap-reset-btn:not(:disabled):hover { color: #d46b08; background: #fff7e6; }
.cap-reset-btn .anticon { font-size: 15px; }
.cap-blocked, .cap-error { color: #d4380d; font-size: 12px; overflow-wrap: anywhere; }
.scenario-editor { display: flex; flex-direction: column; gap: 16px; }
.ed-decl { border: 1px solid #edf0f3; border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; background: #f8fafc; }
.ed-decl-title { font-size: 12px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
.ed-decl-body { font-size: 12px; color: #6b7280; line-height: 1.7; }
.ed-decl-issues { font-size: 12px; color: #d4380d; margin-top: 4px; }
.ed-foot-note { margin-top: 8px; font-size: 12px; color: #9aa4b2; }
.field-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.field { display: flex; min-width: 0; flex-direction: column; gap: 4px; font-size: 13px; color: #4a5568; overflow-wrap: anywhere; }
.field input, .field textarea, .field select {
  width: 100%; min-width: 0; box-sizing: border-box; border: 1px solid #d9dde3; border-radius: 6px; padding: 6px 10px; font: inherit;
}
.field input:disabled, .field textarea:disabled { background: #f5f6f8; color: #7a8491; }
.field textarea { resize: vertical; }
.slot-box { min-width: 0; margin: 0; border: 0; border-top: 1px solid #edf0f3; padding: 16px 0 0; display: flex; flex-direction: column; gap: 16px; }
.slot-box legend { font-size: 13px; font-weight: 600; color: #4a5568; padding: 0 8px 0 0; }
.field-help { font-size: 12px; color: #7a8491; }
.dryrun { display: flex; flex-direction: column; gap: 16px; }
.dryrun-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.dryrun-input { flex: 1 1 260px; min-width: 0; max-width: 460px; border: 1px solid #d9dde3; border-radius: 6px; padding: 7px 10px; font-size: 13px; }
.dryrun-check { flex-shrink: 0; font-size: 13px; color: #4a5568; display: flex; align-items: center; gap: 4px; }
.dryrun-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.dryrun-col { min-width: 0; }
.dryrun-col-title { font-size: 13px; font-weight: 600; color: #4a5568; margin-bottom: 8px; }
.dryrun-pre { margin: 0; background: #f6f7f9; border: 1px solid #edf0f3; border-radius: 8px; padding: 16px; font-size: 12px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.dryrun-pre.changed { border-color: #faad14; background: #fffbe6; }
.dryrun-error { color: #d4380d; font-size: 12px; overflow-wrap: anywhere; }
@container (max-width: 760px) {
  .metro-tab-body { flex-direction: column; }
    .field-grid, .dryrun-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
