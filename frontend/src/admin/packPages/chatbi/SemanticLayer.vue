<template>
  <div class="sl-page">
    <!-- 顶部: 数据源选择 + 搜索 + 版本操作 -->
    <div class="sl-toolbar">
      <a-select v-model:value="dsId" style="min-width: 200px" placeholder="选择数据源"
                :options="datasources.map((d: any) => ({ value: d.id, label: d.name }))"
                @change="loadContent" />
      <a-tag v-if="version" color="success">v{{ version }}</a-tag>
      <a-input-search v-model:value="search" placeholder="搜索表名 / 中文名 / 指标名..." style="width: 260px"
                      allow-clear />
      <div style="margin-left: auto; display: flex; gap: 8px">
        <a-button size="small" @click="loadContent"><ReloadOutlined /> 刷新</a-button>
        <a-button size="small" @click="openVersions"><HistoryOutlined /> 版本历史</a-button>
      </div>
    </div>

    <!-- 空态 -->
    <a-empty v-if="!loading && !models.length" description="该数据源还没有语义层——请先在数据源页扫描"
             style="padding: 80px 0" />

    <!-- 双栏: 左表列表 + 右详情 -->
    <div v-else class="sl-layout">
      <!-- ══ 左栏: 表列表(搜索过滤) ══ -->
      <div class="sl-table-list">
        <div class="sl-list-head">
          <DatabaseOutlined /> 表
          <span class="muted">({{ filteredModels.length }})</span>
        </div>
        <div class="sl-list-body">
          <div
            v-for="m in filteredModels" :key="m.name"
            class="sl-table-item" :class="{ active: selected?.name === m.name }"
            @click="selectTable(m)"
          >
            <div class="sl-table-name">{{ m.display_name }}</div>
            <div class="sl-table-meta">
              <code>{{ m.name }}</code>
              <span class="sl-stats">
                <span class="stat"><b>{{ m.columns.length }}</b>列</span>
                <span class="stat"><b>{{ relCount(m) }}</b>关系</span>
                <span v-if="m.metrics?.length" class="stat"><b>{{ m.metrics.length }}</b>指标</span>
              </span>
            </div>
          </div>
          <div v-if="!filteredModels.length" class="sl-list-empty">无匹配表</div>
        </div>
      </div>

      <!-- ══ 右栏: 表详情 ══ -->
      <div class="sl-detail" v-if="selected">
        <!-- 表头: 中文名编辑 + 来源徽标 -->
        <div class="sl-detail-head">
          <div class="sl-detail-title">
            <template v-if="editingTable">
              <a-input v-model:value="editForm.tableDisplayName" size="small" style="width: 220px"
                       @pressEnter="saveTableEdit" />
              <a-button size="small" type="link" @click="saveTableEdit">保存</a-button>
              <a-button size="small" type="link" @click="editingTable = false">取消</a-button>
            </template>
            <template v-else>
              <b>{{ selected.display_name }}</b>
              <code class="muted">{{ selected.name }}</code>
              <a-button size="small" type="link" @click="startTableEdit"><EditOutlined /> 编辑</a-button>
            </template>
          </div>
          <a-tag :color="sourceTag(selected.source)">{{ sourceLabel(selected.source) }}</a-tag>
        </div>
        <!-- 表描述(可编辑) -->
        <div class="sl-desc-row">
          <a-textarea v-if="editingTable" v-model:value="editForm.tableDescription" :rows="2"
                      placeholder="表的业务描述..." />
          <p v-else-if="selected.description" class="sl-desc">{{ selected.description }}</p>
        </div>

        <!-- 列 -->
        <div class="sl-section">
          <div class="sl-section-title">列 ({{ selected.columns.length }})</div>
          <a-table :data-source="selected.columns" row-key="name" size="small" :pagination="false"
                   class="sl-cols-table">
            <a-table-column title="列名" data-index="name" width="150">
              <template #default="{ record }"><code>{{ record.name }}</code></template>
            </a-table-column>
            <a-table-column title="中文名" width="150">
              <template #default="{ record }">
                <a-input v-if="editingCol === record.name" v-model:value="editForm.colDisplayName"
                         size="small" @pressEnter="commitColEdit(record)" />
                <span v-else class="sl-editable" title="双击编辑"
                      @dblclick="startColEdit(record)">{{ record.display_name }}</span>
              </template>
            </a-table-column>
            <a-table-column title="类型" data-index="data_type" width="130" />
            <a-table-column title="语义" width="130">
              <template #default="{ record }">
                <a-select v-if="editingCol === record.name" v-model:value="editForm.colSemanticType"
                          size="small" style="width: 110px" @change="commitColEdit(record)">
                  <a-select-option value="measure">度量 measure</a-select-option>
                  <a-select-option value="dimension">维度 dimension</a-select-option>
                  <a-select-option value="key">主键 key</a-select-option>
                </a-select>
                <a-tag v-else :color="semTag(record.semantic_type)" class="sl-editable"
                       title="双击编辑" @dblclick="startColEdit(record)">
                  {{ record.semantic_type || '-' }}
                </a-tag>
              </template>
            </a-table-column>
            <a-table-column title="来源" width="110">
              <template #default="{ record }">
                <a-tag :color="sourceTag(record.source)" size="small">
                  {{ sourceLabel(record.source, record.confidence) }}
                </a-tag>
              </template>
            </a-table-column>
          </a-table>
        </div>

        <!-- 关系 -->
        <div v-if="selected.relationships?.length" class="sl-section">
          <div class="sl-section-title">关系 ({{ selected.relationships.length }})</div>
          <a-table :data-source="selected.relationships" row-key="name" size="small" :pagination="false">
            <a-table-column title="基数" data-index="type" width="80" />
            <a-table-column title="目标表" data-index="target_model" width="160">
              <template #default="{ record }"><code>{{ record.target_model }}</code></template>
            </a-table-column>
            <a-table-column title="JOIN" data-index="join_type" width="80" />
            <a-table-column title="ON 条件" data-index="on">
              <template #default="{ record }"><code class="sl-on">{{ record.on }}</code></template>
            </a-table-column>
            <a-table-column title="来源" width="110">
              <template #default="{ record }">
                <a-tag :color="sourceTag(record.source)" size="small">
                  {{ sourceLabel(record.source, record.confidence) }}
                </a-tag>
              </template>
            </a-table-column>
          </a-table>
        </div>

        <!-- 被引用 -->
        <div v-if="reverseRels.length" class="sl-section">
          <div class="sl-section-title">
            被引用 ({{ reverseRels.length }})
            <a-tooltip title="其他表通过外键或推断关系引用了此表">
              <InfoCircleOutlined class="muted" />
            </a-tooltip>
          </div>
          <div v-for="r in reverseRels" :key="r.table" class="sl-rev-line">
            <code>{{ r.table }}</code>
            <span class="muted">{{ r.join_type }} JOIN · {{ r.on }}</span>
            <a-tag size="small" :color="sourceTag(r.source)">{{ sourceLabel(r.source) }}</a-tag>
          </div>
        </div>

        <!-- 指标(可 CRUD) -->
        <div class="sl-section">
          <div class="sl-section-title">
            指标 ({{ selected.metrics?.length || 0 }})
            <a-button size="small" type="link" @click="addMetric"><PlusOutlined /> 新增</a-button>
          </div>
          <a-table v-if="selected.metrics?.length" :data-source="selected.metrics" row-key="name"
                   size="small" :pagination="false">
            <a-table-column title="指标" data-index="display_name" width="130">
              <template #default="{ record }"><b>{{ record.display_name }}</b></template>
            </a-table-column>
            <a-table-column title="公式" data-index="formula">
              <template #default="{ record }"><code class="sl-on">{{ record.formula }}</code></template>
            </a-table-column>
            <a-table-column title="条件" width="160">
              <template #default="{ record }">
                <code v-if="record.condition" class="sl-on">{{ record.condition }}</code>
                <span v-else class="muted">-</span>
              </template>
            </a-table-column>
            <a-table-column title="类型" width="80">
              <template #default="{ record }">
                <a-tag :color="record.type === 'composite' ? 'orange' : 'default'">
                  {{ record.type === 'composite' ? '复合' : '基础' }}
                </a-tag>
              </template>
            </a-table-column>
            <a-table-column title="操作" width="110">
              <template #default="{ record }">
                <a-button size="small" type="link" @click="editMetric(record)">编辑</a-button>
                <a-popconfirm :title="`删除指标「${record.display_name}」?`" ok-text="删除" ok-type="danger"
                              @confirm="deleteMetric(record)">
                  <a-button size="small" type="link" danger>删除</a-button>
                </a-popconfirm>
              </template>
            </a-table-column>
          </a-table>
          <div v-else class="muted sl-metric-empty">暂无指标定义 (扫描时规则/LLM 会自动推断业务指标)</div>
        </div>

        <!-- 示例问题 -->
        <div v-if="content?.sample_questions?.length" class="sl-section">
          <div class="sl-section-title">示例问题</div>
          <a-tag v-for="q in content.sample_questions" :key="q" color="cyan">{{ q }}</a-tag>
        </div>
      </div>
    </div>

    <!-- ══ 版本历史抽屉 ══ -->
    <a-drawer v-model:open="versionDrawer" title="版本历史" width="400">
      <a-spin v-if="versionLoading" class="sl-center" />
      <template v-else>
        <div v-if="!versions.length" class="muted sl-center">暂无历史版本</div>
        <div v-for="v in versions" :key="v.version" class="sl-version-item">
          <div class="sl-version-head">
            <a-tag :color="v.isCurrent ? 'success' : 'default'">
              v{{ v.version }}{{ v.isCurrent ? ' 当前' : '' }}
            </a-tag>
            <span class="muted">{{ fmtTime(v.createdAt) }}</span>
            <div class="sl-version-actions">
              <a-button v-if="!v.isCurrent" size="small" :loading="diffLoading === v.version"
                        @click="showDiff(v.version)">对比当前</a-button>
              <a-button v-if="!v.isCurrent" size="small" danger
                        :loading="rollingBack === v.version"
                        @click="doRollback(v.version)">回滚到此版本</a-button>
            </div>
          </div>
        </div>
      </template>
    </a-drawer>

    <!-- ══ 版本对比抽屉 ══ -->
    <a-drawer v-model:open="diffDrawer" title="版本对比" width="480">
      <template v-if="diffResult">
        <!-- 后端字段是 *_models(原版语义层以 model 称表), 模板沿用 *_tables 文案 -->
        <div v-if="diffResult.added_models?.length" class="sl-diff-section">
          <div class="sl-diff-title add">新增表 ({{ diffResult.added_models.length }})</div>
          <a-tag v-for="t in diffResult.added_models" :key="t" color="success">{{ t }}</a-tag>
        </div>
        <div v-if="diffResult.removed_models?.length" class="sl-diff-section">
          <div class="sl-diff-title remove">删除表 ({{ diffResult.removed_models.length }})</div>
          <a-tag v-for="t in diffResult.removed_models" :key="t" color="error">{{ t }}</a-tag>
        </div>
        <div v-if="diffResult.changed_models?.length" class="sl-diff-section">
          <div class="sl-diff-title change">变更表 ({{ diffResult.changed_models.length }})</div>
          <div v-for="t in diffResult.changed_models" :key="t.table" class="sl-diff-table">
            <b>{{ t.table }}</b>
            <span v-if="t.added_columns?.length" class="sl-diff-col add">+ {{ t.added_columns.join(', ') }}</span>
            <span v-if="t.removed_columns?.length" class="sl-diff-col remove">- {{ t.removed_columns.join(', ') }}</span>
            <span v-if="t.changed_columns?.length" class="sl-diff-col change">~ {{ t.changed_columns.join(', ') }}</span>
          </div>
        </div>
        <div v-if="!diffResult.added_models?.length && !diffResult.removed_models?.length && !diffResult.changed_models?.length"
             class="muted sl-center">两个版本无差异</div>
      </template>
    </a-drawer>

    <!-- ══ 指标编辑弹窗(双栏) ══ -->
    <a-modal v-model:open="metricDialog" :title="metricEditMode === 'add' ? '新增指标' : '编辑指标'"
             ok-text="保存" cancel-text="取消" :confirm-loading="metricSaving" @ok="saveMetric">
      <a-form layout="vertical" class="sl-metric-form">
        <div class="form-row">
          <a-form-item label="标识 (name)" required class="half">
            <a-input v-model:value="metricForm.name" placeholder="英文标识, 如 gmv"
                     :disabled="metricEditMode === 'edit'" />
          </a-form-item>
          <a-form-item label="中文名" required class="half">
            <a-input v-model:value="metricForm.display_name" placeholder="如 成交总额" />
          </a-form-item>
        </div>
        <a-form-item label="公式" required>
          <a-input v-model:value="metricForm.formula" placeholder="如 SUM(total_amount)" />
        </a-form-item>
        <div class="form-row">
          <a-form-item label="类型" class="half">
            <a-select v-model:value="metricForm.type">
              <a-select-option value="single">single (单指标)</a-select-option>
              <a-select-option value="composite">composite (复合指标)</a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item v-if="metricForm.type === 'composite'" label="子指标名" class="half">
            <a-input v-model:value="metricForm.factor_metric_names" placeholder="逗号分隔, 如 gmv, order_count" />
          </a-form-item>
        </div>
        <a-form-item label="过滤条件">
          <a-input v-model:value="metricForm.condition" placeholder="如 status IN ('paid','shipped'), 可选" />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="metricForm.description" :rows="2" placeholder="指标的业务含义, 可选" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 语义层管理子页(对标原版 SemanticView 785 行的核心设计):
//   左表列表(搜索/计数) + 右详情(行内编辑/指标 CRUD/被引用) + 版本历史/回滚/diff。
// 编辑语义 = 整包 PUT(后端 mark_manual_edits 自动打标 manual/1.0, 落新版本)。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  DatabaseOutlined, EditOutlined, HistoryOutlined, InfoCircleOutlined,
  PlusOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'

const props = defineProps<{
  dsId: string                                  // 父级指定数据源
  datasources: { id: string; name: string }[]   // 数据源下拉选项
}>()

const dsId = ref(props.dsId || '')
const loading = ref(false)
const version = ref<number | null>(null)
const content = ref<any>(null)          // SemanticModelContent
const selected = ref<any>(null)         // 当前选中表
const search = ref('')

watch(() => props.dsId, (v) => { if (v) dsId.value = v })
watch(() => props.datasources, (list) => {
  if (!dsId.value && list?.length) dsId.value = list[0].id
})
watch(dsId, (v) => { if (v) loadContent() })

const models = computed(() => content.value?.models || [])

// 搜索过滤: 表名/中文名/指标名(原版同款)
const filteredModels = computed(() => {
  if (!search.value.trim()) return models.value
  const q = search.value.toLowerCase()
  return models.value.filter((m: any) =>
    m.name.toLowerCase().includes(q) ||
    (m.display_name || '').toLowerCase().includes(q) ||
    (m.metrics || []).some((mt: any) =>
      mt.name.toLowerCase().includes(q) || (mt.display_name || '').toLowerCase().includes(q)))
})

// 被引用关系(从语义层全量计算, 不再单独调接口——原版 reverseRelationships 的本地等价)
const reverseRels = computed(() => {
  if (!selected.value) return []
  const target = selected.value.name
  const out: Array<{ table: string; join_type: string; on: string; source: string }> = []
  for (const m of models.value) {
    for (const rel of m.relationships || []) {
      if (rel.target_model === target) {
        out.push({ table: m.name, join_type: rel.join_type, on: rel.on, source: rel.source })
      }
    }
  }
  return out
})

function relCount(m: any): number {
  const outbound = m.relationships?.length || 0
  const inbound = models.value.filter((x: any) =>
    (x.relationships || []).some((r: any) => r.target_model === m.name)).length
  return outbound + inbound
}

function selectTable(m: any) { selected.value = m }

async function loadContent() {
  if (!dsId.value) return
  loading.value = true
  try {
    const { data } = await chatbiApi.get(`/datasources/${dsId.value}/semantic-models`)
    version.value = data.version
    const prevName = selected.value?.name
    content.value = data.content
    // content 整体替换后 selected 必须重绑到新数组里的对象——
    // 否则它还指着旧 content 的表对象, 行内编辑/指标 CRUD 改的是孤儿副本
    // (PUT 发的是新 content, 用户的修改全部丢失)
    selected.value = models.value.find((m: any) => m.name === prevName)
      || models.value[0] || null
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '语义层未就绪')
    content.value = null
    selected.value = null
  } finally {
    loading.value = false
  }
}

// ── 来源徽标(原版同款文案) ──
function sourceLabel(source: string, confidence?: number): string {
  const map: Record<string, string> = {
    manual: '📋 注释', foreign_key: '🔑 外键',
    auto_inferred: (confidence ?? 1) >= 0.8 ? '🤖 LLM' : '⚠️ 退化',
    name_pattern: '🔤 命名', ai_inferred: '🤖 LLM', rule_inferred: '⚙️ 规则',
  }
  return map[source] || source || '-'
}
function sourceTag(source: string): string {
  const map: Record<string, string> = {
    manual: 'success', foreign_key: 'warning', auto_inferred: 'processing',
    name_pattern: 'processing', ai_inferred: 'processing', rule_inferred: 'default',
  }
  return map[source] || 'default'
}
function semTag(t?: string): string {
  return { measure: 'blue', dimension: 'green', key: 'purple' }[t || ''] || 'default'
}
function fmtTime(iso?: string): string {
  return iso ? iso.replace('T', ' ').slice(0, 16) : ''
}

// ── 行内编辑(编辑 = 修改本地 content 后整包 PUT, 后端 diff 打标 manual) ──
const editingTable = ref(false)
const editingCol = ref<string | null>(null)
const editForm = reactive({ tableDisplayName: '', tableDescription: '',
                            colDisplayName: '', colSemanticType: '' })
const saving = ref(false)

function startTableEdit() {
  if (!selected.value) return
  editForm.tableDisplayName = selected.value.display_name || ''
  editForm.tableDescription = selected.value.description || ''
  editingTable.value = true
}

async function saveTableEdit() {
  if (!selected.value) return
  if (editForm.tableDisplayName === (selected.value.display_name || '') &&
      editForm.tableDescription === (selected.value.description || '')) {
    editingTable.value = false
    return
  }
  selected.value.display_name = editForm.tableDisplayName
  selected.value.description = editForm.tableDescription
  editingTable.value = false
  await persist('表信息已更新')
}

function startColEdit(record: any) {
  editingCol.value = record.name
  editForm.colDisplayName = record.display_name || ''
  editForm.colSemanticType = record.semantic_type || 'dimension'
}

// 回车提交: 只走这一条路径(blur 双触发是拼接脏数据的根因——pressEnter 后
// input 失焦又触发一次 blur 保存, 两次都拿"旧值+新值拼接"的中间态)
function commitColEdit(record: any) {
  const col = editingCol.value
  editingCol.value = null
  if (!col || !selected.value) return
  if (editForm.colDisplayName === (record.display_name || '') &&
      editForm.colSemanticType === (record.semantic_type || '')) return
  const target = selected.value.columns.find((c: any) => c.name === col)
  if (target) {
    target.display_name = editForm.colDisplayName
    target.semantic_type = editForm.colSemanticType
  }
  void persist('列信息已更新')
}

// 整包 PUT(后端 mark_manual_edits 与校验都在 API 层)
async function persist(okMsg: string) {
  if (!dsId.value || !content.value) return
  saving.value = true
  try {
    const { data } = await chatbiApi.put(
      `/datasources/${dsId.value}/semantic-models`,
      { content: content.value, expected_version: version.value ?? 0 })
    version.value = data.version
    if (data.index_rebuilt === false) {
      message.warning(`${okMsg} (v${data.version}) — 但索引重建失败: ${data.warning || '请重扫恢复'}`)
    } else {
      message.success(`${okMsg} (v${data.version})`)
    }
  } catch (e: any) {
    if (e?.response?.status === 409 || e?.response?.status === 428) {
      // 十审 7.7: 保留本地草稿——不 loadContent 覆盖用户编辑
      message.warning('内容已被其他管理员更新——您的修改仍在本页, 可复制后刷新对比', 6)
    } else {
      message.error(e?.response?.data?.detail || '保存失败')
      await loadContent()   // 非 409 失败回滚到服务端状态
    }
  } finally {
    saving.value = false
  }
}

// ── 指标 CRUD(本地改 + persist) ──
const metricDialog = ref(false)
const metricEditMode = ref<'add' | 'edit'>('add')
const metricSaving = ref(false)
const metricForm = reactive({
  name: '', display_name: '', formula: '', type: 'single',
  condition: '', description: '', factor_metric_names: '',
})

function addMetric() {
  metricEditMode.value = 'add'
  Object.assign(metricForm, { name: '', display_name: '', formula: '', type: 'single',
                              condition: '', description: '', factor_metric_names: '' })
  metricDialog.value = true
}

function editMetric(row: any) {
  metricEditMode.value = 'edit'
  Object.assign(metricForm, {
    name: row.name, display_name: row.display_name, formula: row.formula,
    type: row.type || 'single', condition: row.condition || '',
    description: row.description || '',
    factor_metric_names: (row.factor_metric_names || []).join(', '),
  })
  metricDialog.value = true
}

async function saveMetric() {
  if (!selected.value) return
  if (!metricForm.name.trim() || !metricForm.display_name.trim() || !metricForm.formula.trim()) {
    message.warning('请填写标识、中文名和公式')
    return
  }
  if (metricForm.type === 'composite' && !metricForm.factor_metric_names.trim()) {
    message.warning('composite 指标需填写子指标名')
    return
  }
  metricSaving.value = true
  try {
    const factors = metricForm.factor_metric_names.split(',').map(s => s.trim()).filter(Boolean)
    const metric: any = {
      name: metricForm.name.trim(), display_name: metricForm.display_name.trim(),
      formula: metricForm.formula.trim(), type: metricForm.type,
      condition: metricForm.condition || null, description: metricForm.description || null,
      factor_metric_names: factors.length ? factors : null,
      // Metric 模型 extra=forbid: 只发已声明字段(confidence 不在模型里)
      co_occurrence: 0, source: 'manual',
    }
    const list = selected.value.metrics || (selected.value.metrics = [])
    const idx = list.findIndex((m: any) => m.name === metric.name)
    if (idx >= 0) list.splice(idx, 1, metric)
    else list.push(metric)
    metricDialog.value = false
    await persist('指标已保存')
  } finally {
    metricSaving.value = false
  }
}

async function deleteMetric(row: any) {
  if (!selected.value) return
  selected.value.metrics = (selected.value.metrics || []).filter((m: any) => m.name !== row.name)
  await persist('指标已删除')
}

// ── 版本历史 / 回滚 / diff ──
const versionDrawer = ref(false)
const versionLoading = ref(false)
const versions = ref<any[]>([])
const rollingBack = ref<number | null>(null)
const diffDrawer = ref(false)
const diffLoading = ref<number | null>(null)
const diffResult = ref<any>(null)

async function openVersions() {
  if (!dsId.value) return
  versionDrawer.value = true
  versionLoading.value = true
  try {
    const { data } = await chatbiApi.get(`/datasources/${dsId.value}/semantic-versions`)
    versions.value = data.items || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '加载版本历史失败')
  } finally {
    versionLoading.value = false
  }
}

async function doRollback(toVersion: number) {
  rollingBack.value = toVersion
  try {
    const { data } = await chatbiApi.post('/semantic-rollback', null, {
      params: { ds_id: dsId.value, version: toVersion,
                expected_current_version: version.value ?? 0 } })
    if (data.index_rebuilt === false) {
      message.warning(`已回滚到 v${toVersion} — 但索引重建失败: ${data.warning || '请重扫恢复'}`)
    } else {
      message.success(`已回滚到 v${toVersion}`)
    }
    versionDrawer.value = false
    await loadContent()
  } catch (e: any) {
    e?.response?.status === 409
        ? message.warning('回滚期间版本被并发修改——请刷新后重试')
        : message.error(e?.response?.data?.detail || '回滚失败')
  } finally {
    rollingBack.value = null
  }
}

async function showDiff(fromVersion: number) {
  diffLoading.value = fromVersion
  diffDrawer.value = true
  diffResult.value = null
  try {
    const { data } = await chatbiApi.get('/semantic-diff', {
      params: { ds_id: dsId.value, from_version: fromVersion, to_version: version.value } })
    diffResult.value = data.diff
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '对比失败')
    diffDrawer.value = false
  } finally {
    diffLoading.value = null
  }
}

onMounted(() => {
  if (dsId.value) loadContent()
  else if (props.datasources?.length) dsId.value = props.datasources[0].id
})
</script>

<style scoped>
.sl-page { display: flex; flex-direction: column; gap: 12px; height: calc(100vh - 210px); min-height: 480px; }
.sl-toolbar { display: flex; align-items: center; gap: 10px; }
.muted { color: #86909c; font-size: 12px; }

/* 双栏布局 */
.sl-layout { flex: 1; display: flex; gap: 12px; overflow: hidden; }

/* 左栏表列表 */
.sl-table-list {
  width: 270px; flex-shrink: 0; display: flex; flex-direction: column;
  background: #fff; border: 1px solid #eef1f7; border-radius: 10px; overflow: hidden;
}
.sl-list-head {
  padding: 10px 14px; font-weight: 600; font-size: 13px; color: #1f2937;
  border-bottom: 1px solid #f0f1f3; display: flex; align-items: center; gap: 6px;
}
.sl-list-body { flex: 1; overflow-y: auto; padding: 6px; }
.sl-table-item {
  padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px;
  transition: background 0.15s;
}
.sl-table-item:hover { background: #f7f8fa; }
.sl-table-item.active { background: #eaf0ff; }
.sl-table-item.active .sl-table-name { color: #3370ff; }
.sl-table-name { font-size: 13px; font-weight: 600; color: #1f2937; }
.sl-table-meta { display: flex; align-items: center; gap: 6px; margin-top: 3px; }
.sl-table-meta code { font-size: 11px; color: #86909c; }
.sl-stats { margin-left: auto; display: flex; gap: 6px; }
.sl-stats .stat { font-size: 11px; color: #86909c; }
.sl-stats b { color: #4e5969; font-weight: 600; }
.sl-list-empty { padding: 30px 0; text-align: center; color: #c9cdd4; font-size: 12px; }

/* 右栏详情 */
.sl-detail {
  flex: 1; overflow-y: auto; background: #fff; border: 1px solid #eef1f7;
  border-radius: 10px; padding: 16px 18px;
}
.sl-detail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.sl-detail-title { display: flex; align-items: center; gap: 8px; font-size: 16px; }
.sl-detail-title code { font-size: 12px; }
.sl-desc-row { margin: 6px 0 2px; }
.sl-desc { font-size: 13px; color: #4e5969; margin: 4px 0; }

.sl-section { margin-top: 18px; }
.sl-section-title {
  font-size: 13px; font-weight: 600; color: #1f2937; margin-bottom: 8px;
  display: flex; align-items: center; gap: 6px;
}
.sl-editable { cursor: pointer; border-bottom: 1px dashed transparent; }
.sl-editable:hover { border-bottom-color: #3370ff; }
.sl-on { font-size: 12px; word-break: break-all; }
.sl-rev-line {
  display: flex; align-items: center; gap: 10px; padding: 6px 0;
  border-bottom: 1px solid #f5f5f6; font-size: 12px;
}
.sl-metric-empty { font-size: 12px; color: #86909c; padding: 6px 0; }
.sl-cols-table :deep(code) { font-size: 12px; }

/* 版本抽屉 */
.sl-center { display: flex; justify-content: center; padding: 40px 0; }
.sl-version-item { padding: 10px 0; border-bottom: 1px solid #f0f1f3; }
.sl-version-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sl-version-actions { margin-left: auto; display: flex; gap: 6px; }

/* diff 抽屉 */
.sl-diff-section { margin-bottom: 14px; }
.sl-diff-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.sl-diff-title.add { color: #00a870; }
.sl-diff-title.remove { color: #f54a45; }
.sl-diff-title.change { color: #ff9e29; }
.sl-diff-table { padding: 6px 0; border-bottom: 1px solid #f5f5f6; font-size: 12px; }
.sl-diff-col { display: block; margin-top: 2px; }
.sl-diff-col.add { color: #00a870; }
.sl-diff-col.remove { color: #f54a45; }

/* 指标弹窗双栏 */
.sl-metric-form .form-row { display: flex; gap: 12px; }
.sl-metric-form .half { flex: 1; }
</style>
