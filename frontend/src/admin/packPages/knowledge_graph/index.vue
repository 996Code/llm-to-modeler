<template>
  <div class="kg-page">
    <!-- 顶部知识库工作条:选择 + 统计 + 管理(新建/删除)合一 -->
    <div class="kg-bar">
      <div class="kg-bar-title">
        <PartitionOutlined class="kg-bar-icon" />
        <span>知识库</span>
      </div>
      <a-select
        v-model:value="currentKbId" style="min-width: 240px" placeholder="选择知识库"
        :loading="loadingKbs" @change="onKbChange"
      >
        <a-select-option v-for="k in kbs" :key="k.id" :value="k.id">
          {{ k.name }}
          <span class="kg-bar-meta">{{ k.docCount ?? 0 }} 文档 / {{ k.entityTotal ?? 0 }} 实体</span>
        </a-select-option>
      </a-select>
      <a-button type="primary" @click="openCreate"><PlusOutlined /> 新建</a-button>
      <a-popconfirm
        v-if="currentKb"
        title="删除当前知识库将同时清除图谱、向量与上传文件,确认?"
        ok-text="删除" ok-type="danger" @confirm="doDeleteCurrent"
      >
        <a-button danger><DeleteOutlined /> 删除</a-button>
      </a-popconfirm>
      <a-button @click="refreshAll"><SyncOutlined :spin="loadingKbs" /> 刷新</a-button>
    </div>

    <!-- 当前库信息条:描述/标签/统计(替代原管理页卡片的职责) -->
    <div v-if="currentKb" class="kg-info">
      <div class="kg-info-main">
        <span class="kg-info-name">{{ currentKb.name }}</span>
        <a-tag v-if="currentKb.vectorEnabled" color="cyan">向量 dim={{ currentKb.vectorDim }}</a-tag>
        <a-tag v-else>纯图谱</a-tag>
        <a-tag v-if="currentKb.schemaTemplate" color="geekblue">{{ currentKb.schemaTemplate }}</a-tag>
        <span v-if="currentKb.description" class="kg-info-desc">{{ currentKb.description }}</span>
      </div>
      <div class="kg-info-stats">
        <span><FileTextOutlined /> 文档 <b>{{ currentKb.docCount ?? 0 }}</b></span>
        <span><NodeIndexOutlined /> 实体 <b>{{ currentKb.entityTotal ?? 0 }}</b></span>
        <span><ApartmentOutlined /> 关系 <b>{{ currentKb.relationTotal ?? 0 }}</b></span>
      </div>
    </div>

    <!-- 无库空态:引导新建 -->
    <div v-if="!loadingKbs && !kbs.length" class="kg-empty" @click="openCreate">
      <PartitionOutlined class="kg-empty-icon" />
      <div class="kg-empty-title">还没有知识库</div>
      <div class="kg-empty-sub">每个库独立本体 / 图谱 / 向量 collection(物理隔离,删库即清)——点这里创建第一个</div>
    </div>

    <a-tabs v-else v-model:activeKey="tab" size="small">
      <a-tab-pane key="docs">
        <template #tab><FileTextOutlined /> 文档导入</template>
        <DocManager v-if="currentKbId" :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
        <a-empty v-else description="先在上方选择或新建一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
      <a-tab-pane key="graph">
        <template #tab><NodeIndexOutlined /> 图谱浏览</template>
        <GraphView v-if="currentKbId" :kb-id="currentKbId" :schema="currentKb?.schema || null" :refresh-tick="refreshTick" />
        <a-empty v-else description="先在上方选择或新建一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
      <a-tab-pane key="schema">
        <template #tab><ApartmentOutlined /> 本体设置</template>
        <SchemaEditor v-if="currentKbId" :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
        <a-empty v-else description="先在上方选择或新建一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
    </a-tabs>

    <!-- 建库弹窗:选本体模板起步(自原库管理页并入) -->
    <a-modal v-model:open="createOpen" title="新建知识库" :confirm-loading="creating" @ok="doCreate">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" placeholder="如:产品手册、组织人事库" :maxlength="60" />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="form.description" placeholder="可选" :maxlength="200" />
        </a-form-item>
        <a-form-item label="本体模板(建库后可在「本体设置」页完全自定义)">
          <div class="kg-templates">
            <div
              v-for="t in templates" :key="t.key" class="kg-tpl"
              :class="{ on: form.template === t.key }" @click="form.template = t.key"
            >
              <div class="kg-tpl-title">{{ t.title }}</div>
              <div class="kg-tpl-desc">{{ t.description }}</div>
              <div class="kg-tpl-meta">{{ t.entityCount }} 实体类型 / {{ t.relationCount }} 关系类型</div>
            </div>
          </div>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 知识图谱管理页(manifest admin.page=knowledge_graph → AdminApp 动态 Tab 挂载)。
// 结构:顶部"知识库工作条"(选择 + 统计 + 新建/删除,合并自原「知识库管理」子页)
//       + 三个子页(文档导入/图谱浏览/本体设置)。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ApartmentOutlined, DeleteOutlined, FileTextOutlined,
  NodeIndexOutlined, PartitionOutlined, PlusOutlined, SyncOutlined,
} from '@ant-design/icons-vue'
import {
  KgKnowledgeBase, KgTemplate, createKgKb, deleteKgKb, fetchKgKbs, fetchKgTemplates,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import DocManager from './DocManager.vue'
import SchemaEditor from './SchemaEditor.vue'
import GraphView from './GraphView.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

const kbs = ref<KgKnowledgeBase[]>([])
const currentKbId = ref('')
const loadingKbs = ref(false)
const tab = ref('docs')
// 刷新联动:点「刷新」时自增,子页(文档/图谱/本体)watch 它重载
const refreshTick = ref(0)

const currentKb = computed(() => kbs.value.find((k) => k.id === currentKbId.value))

function refreshAll() {
  refreshTick.value++
  loadKbs()
}

let loadKbsSeq = 0   // 请求序号守卫:旧响应晚到不得清掉刚选中的库(如新建后)

async function loadKbs() {
  const seq = ++loadKbsSeq
  loadingKbs.value = true
  await loadSafely(async () => {
    const items = await fetchKgKbs()
    if (seq !== loadKbsSeq) return   // 已有更新请求,丢弃过期结果
    kbs.value = items
    if (currentKbId.value && !kbs.value.some((k) => k.id === currentKbId.value)) {
      currentKbId.value = ''
    }
    if (!currentKbId.value && kbs.value.length === 1) {
      currentKbId.value = kbs.value[0].id
    }
  })
  if (seq === loadKbsSeq) loadingKbs.value = false
}

function onKbChange() {
  // 切库时子页按 kbId prop 自行重载(响应式),这里无需额外动作
}

// ── 建库/删库(自 KbManager 并入) ──────────────────────
const templates = ref<KgTemplate[]>([])
const createOpen = ref(false)
const creating = ref(false)
const form = ref({ name: '', description: '', template: 'general' })

onMounted(async () => {
  loadKbs()
  await loadSafely(async () => {
    templates.value = await fetchKgTemplates()
  })
})

function openCreate() {
  form.value = { name: '', description: '', template: 'general' }
  createOpen.value = true
}

async function doCreate() {
  if (!form.value.name.trim()) {
    message.warning('请输入知识库名称')
    return
  }
  creating.value = true
  await loadSafely(async () => {
    const kb = await createKgKb({
      name: form.value.name.trim(),
      description: form.value.description.trim(),
      template: form.value.template,
    })
    message.success(`知识库「${kb.name}」已创建`)
    createOpen.value = false
    currentKbId.value = kb.id
    tab.value = 'docs'
    loadKbs()
  })
  creating.value = false
}

async function doDeleteCurrent() {
  if (!currentKb.value) return
  const kb = currentKb.value
  await loadSafely(async () => {
    const r = await deleteKgKb(kb.id)
    if (r.cleanupErrors?.length) {
      message.warning(`已删除,但部分存储清理失败:${r.cleanupErrors.join('; ')}(可重试)`)
    } else {
      message.success(`知识库「${kb.name}」已删除`)
    }
    currentKbId.value = ''
    loadKbs()
  })
}
</script>

<style scoped>
.kg-page { padding: 6px 2px 0; }
.kg-bar {
  display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  background: linear-gradient(90deg, #f5f7ff, #fafbff);
  border: 1px solid #e6ebf5; border-radius: 10px; padding: 10px 16px;
}
.kg-bar-title { display: flex; align-items: center; gap: 6px; font-weight: 600; color: #1f2937; }
.kg-bar-icon { color: #2f54eb; font-size: 16px; }
.kg-bar-meta { color: #94a3b8; font-size: 12px; margin-left: 8px; }

.kg-info {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  border: 1px solid #eef1f7; border-radius: 10px; padding: 8px 16px;
  margin-bottom: 12px; background: #fff;
}
.kg-info-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.kg-info-name { font-weight: 700; font-size: 13.5px; color: #1f2937; }
.kg-info-desc { color: #9ca3af; font-size: 12px; max-width: 460px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kg-info-stats { margin-left: auto; display: flex; gap: 16px; }
.kg-info-stats span { font-size: 12px; color: #6b7280; display: flex; align-items: center; gap: 4px; }
.kg-info-stats b { color: #1f2937; font-size: 14px; }

.kg-empty {
  border: 1.5px dashed #c7d2fe; border-radius: 12px; padding: 46px 20px; text-align: center;
  cursor: pointer; background: #fafbff; transition: border-color 0.15s, background 0.15s;
}
.kg-empty:hover { border-color: #2f54eb; background: #f5f7ff; }
.kg-empty-icon { font-size: 30px; color: #2f54eb; }
.kg-empty-title { font-weight: 700; font-size: 15px; color: #1f2937; margin-top: 8px; }
.kg-empty-sub { font-size: 12.5px; color: #9ca3af; margin-top: 4px; }

.kg-templates { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.kg-tpl {
  border: 1px solid #e6ebf2; border-radius: 10px; padding: 10px 12px; cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.kg-tpl:hover { border-color: #a5b4fc; }
.kg-tpl.on { border-color: #2f54eb; background: #f5f7ff; }
.kg-tpl-title { font-weight: 600; font-size: 13px; color: #1f2937; }
.kg-tpl-desc { font-size: 12px; color: #6b7280; margin: 4px 0 2px; }
.kg-tpl-meta { font-size: 11px; color: #94a3b8; }
</style>
