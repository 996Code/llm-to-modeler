<template>
  <div v-if="schema" class="se-page">
    <!-- 模式与操作 -->
    <div class="se-toolbar">
      <span class="se-label">抽取模式</span>
      <a-radio-group v-model:value="schema.schema_mode" button-style="solid" size="small">
        <a-radio-button value="semi_open">半开放(类型外数据进待审)</a-radio-button>
        <a-radio-button value="strict">严格(类型外数据丢弃)</a-radio-button>
      </a-radio-group>
      <span class="se-label" style="margin-left: 16px">向量</span>
      <a-tag :color="kbVector ? 'cyan' : 'default'">
        {{ kbVector ? `启用 dim=${kbDim}` : '未启用(导入时探测 LLM_EMBED_MODEL)' }}
      </a-tag>
      <div class="se-spacer" />
      <a-button @click="doInduce"><BulbOutlined /> LLM 归纳本体</a-button>
      <a-button type="primary" :loading="saving" @click="doSave"><SaveOutlined /> 保存</a-button>
    </div>

    <!-- 归纳提案(待审) -->
    <a-collapse v-if="induction" class="se-induction" :active-key="inductionOpen">
      <a-collapse-panel key="on" header="待审:LLM 归纳的本体提案(应用后覆盖现有类型表)">
        <template #extra>
          <a-space @click.stop>
            <a-button size="small" type="primary" @click="applyInduction"><CheckOutlined /> 应用提案</a-button>
            <a-button size="small" @click="dismissInduction"><CloseOutlined /> 丢弃</a-button>
          </a-space>
        </template>
        <div class="se-tpl-preview">
          <div>
            <div class="se-sub-title">实体类型({{ induction.entity_types.length }})</div>
            <div v-for="t in induction.entity_types" :key="t.key" class="se-pill">
              <b>{{ t.label }}</b> <code>{{ t.key }}</code> {{ t.description }}
            </div>
          </div>
          <div>
            <div class="se-sub-title">关系类型({{ induction.relation_types.length }})</div>
            <div v-for="r in induction.relation_types" :key="r.key" class="se-pill">
              <b>{{ r.label }}</b> <code>{{ r.key }}</code>
              {{ r.domain?.length ? `${r.domain.join('/')} → ${r.range?.join('/')}` : r.description }}
            </div>
          </div>
        </div>
      </a-collapse-panel>
    </a-collapse>

    <!-- 待审单项提案(semi_open 抽取产生) -->
    <a-alert
      v-if="pendingTypes.length" type="warning" show-icon style="margin-bottom: 12px"
      :message="`抽取过程发现 ${pendingTypes.length} 个类型外新类型,待审核`"
    >
      <template #description>
        <div class="se-pending-row">
          <div class="se-pending-list">
            <a-tag v-for="(p, i) in pendingTypes" :key="i" :color="p.kind === 'entity' ? 'geekblue' : 'purple'">
              {{ p.kind === 'entity' ? '实体' : '关系' }}:{{ p.key }}
            </a-tag>
          </div>
          <a-space>
            <a-button size="small" @click="approveAllPending"><CheckOutlined /> 全部并入</a-button>
            <a-button size="small" @click="schema.pending_types = []"><CloseOutlined /> 全部丢弃</a-button>
          </a-space>
        </div>
      </template>
    </a-alert>

    <!-- 类型编辑表 -->
    <div class="se-tables">
      <div class="se-table">
        <div class="se-table-head">
          <span>实体类型({{ entityTypes.length }})</span>
          <a-button size="small" type="dashed" @click="addEntity"><PlusOutlined /> 加类型</a-button>
        </div>
        <div class="se-rows">
          <div v-for="(t, i) in entityTypes" :key="i" class="se-row">
            <input v-model="t.label" class="se-in se-in-label" placeholder="中文名" />
            <input v-model="t.key" class="se-in se-in-key" placeholder="key" />
            <input v-model="t.description" class="se-in se-in-desc" placeholder="说明(给抽取用)" />
            <div class="se-color" :style="{ background: t.color || '#cbd5e1' }"
                 :title="`类型色:${t.color || '未设置'}`">
              <input v-model="t.color" type="color" class="se-color-input" />
            </div>
            <a class="se-del" @click="entityTypes.splice(i, 1)"><DeleteOutlined /></a>
          </div>
        </div>
      </div>

      <div class="se-table">
        <div class="se-table-head">
          <span>关系类型({{ relationTypes.length }})</span>
          <a-button size="small" type="dashed" @click="addRelation"><PlusOutlined /> 加类型</a-button>
        </div>
        <div class="se-rows">
          <div v-for="(r, i) in relationTypes" :key="i" class="se-row">
            <input v-model="r.label" class="se-in se-in-label" placeholder="中文名" />
            <input v-model="r.key" class="se-in se-in-key" placeholder="key" />
            <input v-model="r.description" class="se-in se-in-desc" placeholder="说明" />
            <input v-model="r.domain" class="se-in se-in-dr" placeholder="源类型(逗号)" />
            <span class="se-arrow">→</span>
            <input v-model="r.range" class="se-in se-in-dr" placeholder="目标类型(逗号)" />
            <a class="se-del" @click="relationTypes.splice(i, 1)"><DeleteOutlined /></a>
          </div>
        </div>
        <div class="se-note">domain/range 填实体类型 key(逗号分隔,留空 = 不限制);约束注入抽取 prompt</div>
      </div>
    </div>
  </div>
  <a-empty v-else description="该库没有本体数据(异常:建库时未套模板)" style="padding: 60px 0" />
</template>

<script setup lang="ts">
// 本体编辑:模式切换 + 实体/关系类型表编辑(颜色供图谱可视化) +
// semi_open 待审类型 + LLM 归纳提案应用/丢弃。
import { computed, inject, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  BulbOutlined, CheckOutlined, CloseOutlined, DeleteOutlined, PlusOutlined, SaveOutlined,
} from '@ant-design/icons-vue'
import {
  KgKnowledgeBase, KgRelationTypeDef, KgSchema, KgEntityTypeDef,
  fetchKgKbs, induceKgSchema, updateKgKb,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string; refreshTick?: number }>()
const emit = defineEmits<{ (e: 'changed'): void }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const schema = ref<KgSchema | null>(null)
const kbVector = ref(false)
const kbDim = ref<number | null>(null)
const saving = ref(false)
const inductionOpen = ref<string[]>([])

const entityTypes = ref<KgEntityTypeDef[]>([])
const relationTypes = ref<KgRelationTypeDef[]>([])
const pendingTypes = computed(() => schema.value?.pending_types || [])
const induction = computed(() => schema.value?.pending_schema_induction || null)

async function load() {
  await loadSafely(async () => {
    const kbs = await fetchKgKbs()
    const kb = kbs.find((k) => k.id === props.kbId) as KgKnowledgeBase | undefined
    if (!kb) return
    schema.value = { ...(kb.schema || { schema_mode: 'semi_open', entity_types: [], relation_types: [], pending_types: [] }) }
    // 深拷贝到可编辑副本
    entityTypes.value = (schema.value.entity_types || []).map((t) => ({ ...t }))
    relationTypes.value = (schema.value.relation_types || []).map((r) => ({ ...r }))
    kbVector.value = kb.vectorEnabled
    kbDim.value = kb.vectorDim
    inductionOpen.value = schema.value.pending_schema_induction ? ['on'] : []
  })
}

function addEntity() {
  entityTypes.value.push({ key: '', label: '', description: '', examples: [], color: '#5470c6' })
}
function addRelation() {
  relationTypes.value.push({ key: '', label: '', description: '', domain: [], range: [] })
}

function _splitList(v: unknown): string[] {
  if (Array.isArray(v)) return v
  return String(v || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean)
}

async function doSave() {
  if (!schema.value) return
  if (entityTypes.value.some((t) => !t.key.trim() || !t.label.trim())) {
    message.warning('实体类型的 key 与中文名不能为空')
    return
  }
  if (relationTypes.value.some((r) => !r.key.trim() || !r.label.trim())) {
    message.warning('关系类型的 key 与中文名不能为空')
    return
  }
  saving.value = true
  await loadSafely(async () => {
    await updateKgKb(props.kbId, {
      schema: {
        ...schema.value!,
        entity_types: entityTypes.value.map((t) => ({ ...t, key: t.key.trim() })),
        relation_types: relationTypes.value.map((r) => ({
          ...r, key: r.key.trim(), domain: _splitList(r.domain), range: _splitList(r.range),
        })),
      },
    })
    message.success('本体已保存(对后续导入生效;存量数据可「强制」重导刷新)')
  })
  saving.value = false
  await load()
  emit('changed')
}

function applyInduction() {
  if (!schema.value || !induction.value) return
  schema.value.entity_types = induction.value.entity_types.map((t) => ({ ...t }))
  schema.value.relation_types = induction.value.relation_types.map((r) => ({ ...r }))
  schema.value.pending_schema_induction = undefined
  entityTypes.value = (schema.value.entity_types || []).map((t) => ({ ...t }))
  relationTypes.value = (schema.value.relation_types || []).map((r) => ({ ...r }))
  inductionOpen.value = []
  message.info('已套用归纳提案(点「保存」落库)')
}

function dismissInduction() {
  if (!schema.value) return
  schema.value.pending_schema_induction = undefined
  inductionOpen.value = []
}

function approveAllPending() {
  if (!schema.value) return
  for (const p of schema.value.pending_types || []) {
    if (p.kind === 'entity') {
      entityTypes.value.push({ key: p.key, label: p.label, description: 'LLM 提案', examples: [] })
    } else {
      relationTypes.value.push({ key: p.key, label: p.label, description: 'LLM 提案', domain: [], range: [] })
    }
  }
  schema.value.pending_types = []
  message.info('已并入类型表(点「保存」落库)')
}

async function doInduce() {
  await loadSafely(async () => {
    const task = await induceKgSchema(props.kbId)
    message.info(`归纳任务已提交(${task.id.slice(0, 8)}…),完成后此处出现待审提案;日志见任务中心`)
    // 轮询任务结束后重载本体
    const poll = window.setInterval(async () => {
      try {
        const { fetchTask } = await import('../../api')
        const t = await fetchTask(task.id)
        if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(t.status)) {
          window.clearInterval(poll)
          await load()
          if (t.status === 'succeeded') message.success('本体归纳完成,请审核提案')
        }
      } catch { /* 继续 */ }
    }, 2000)
  })
}

watch(() => props.kbId, load)
watch(() => props.refreshTick, load)
onMounted(load)
</script>

<style scoped>
.se-page { padding: 2px 0; }
.se-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.se-label { font-size: 13px; color: #4b5563; font-weight: 600; }
.se-spacer { flex: 1; }
.se-induction { margin-bottom: 12px; }
.se-tpl-preview { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.se-sub-title { font-weight: 600; font-size: 12.5px; color: #1f2937; margin-bottom: 6px; }
.se-pill { font-size: 12px; color: #4b5563; padding: 3px 0; }
.se-pill code { background: #f1f5f9; padding: 0 4px; border-radius: 3px; color: #2f54eb; }
.se-pending-row { display: flex; align-items: center; gap: 14px; }
.se-pending-list { display: flex; gap: 6px; flex-wrap: wrap; }
.se-tables { display: grid; grid-template-columns: 1fr; gap: 14px; }
.se-table { border: 1px solid #e6ebf2; border-radius: 10px; padding: 10px 12px; background: #fff; }
.se-table-head {
  display: flex; justify-content: space-between; align-items: center;
  font-weight: 600; font-size: 13px; color: #1f2937; margin-bottom: 8px;
}
.se-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.se-in {
  border: 1px solid #e2e8f0; border-radius: 6px; padding: 4px 8px; font-size: 12.5px;
  color: #1f2937; background: #fff; outline: none;
}
.se-in:focus { border-color: #2f54eb; }
.se-in-label { width: 110px; }
.se-in-key { width: 120px; font-family: 'SF Mono', Menlo, Consolas, monospace; color: #2f54eb; }
.se-in-desc { flex: 1; min-width: 120px; }
.se-in-dr { width: 130px; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 11.5px; }
.se-arrow { color: #94a3b8; }
.se-color { width: 26px; height: 26px; border-radius: 6px; border: 1px solid #e2e8f0; overflow: hidden; position: relative; flex-shrink: 0; }
.se-color-input { position: absolute; inset: -6px; width: 40px; height: 40px; opacity: 0; cursor: pointer; }
.se-del { color: #dc2626; margin-left: 4px; flex-shrink: 0; }
.se-note { font-size: 11.5px; color: #94a3b8; margin-top: 4px; }
</style>
