<template>
  <div class="kg-page">
    <!-- 左右分栏:左侧知识库列表(替代原顶部下拉工作条),右侧选中库详情 -->
    <div class="kg-split">
      <!-- 左侧:知识库树(库名 + 文档/实体统计) -->
      <aside class="kg-side">
        <div class="kg-side-head">
          <div class="kg-side-title"><PartitionOutlined class="kg-side-icon" /> 知识库</div>
          <a-button size="small" type="primary" @click="openCreate"><PlusOutlined /> 新建</a-button>
        </div>
        <div class="kg-side-list">
          <div v-if="loadingKbs && !kbs.length" class="kg-side-tip">加载中…</div>
          <template v-else>
            <div
              v-for="k in kbs" :key="k.id" class="kb-item"
              :class="{ on: k.id === currentKbId }" @click="currentKbId = k.id"
            >
              <div class="kb-item-name" :title="k.name">{{ k.name }}</div>
              <div class="kb-item-meta">{{ k.docCount ?? 0 }} 文档 / {{ k.entityTotal ?? 0 }} 实体</div>
            </div>
            <!-- 列表空态:引导新建 -->
            <div v-if="!kbs.length" class="kb-item-empty" @click="openCreate">
              <PlusOutlined class="kb-item-empty-icon" />
              <div>还没有知识库<br>点此创建第一个</div>
            </div>
          </template>
        </div>
      </aside>

      <!-- 右侧:选中库详情——信息条(含删除/刷新,原工作条拆解并入) + 四个子页 tab -->
      <section class="kg-main">
        <template v-if="currentKb">
          <div class="kg-info">
            <div class="kg-info-main">
              <span class="kg-info-name">{{ currentKb.name }}</span>
              <a-tag v-if="currentKb.vectorEnabled" color="cyan">向量 dim={{ currentKb.vectorDim || '待导入探测' }}</a-tag>
              <!-- 默认启用后仅剩"模型变更保护"会走到这——不再是无配置的未决态 -->
              <a-tooltip v-else title="embedding 模型与建库时不一致,向量已保护性停用——切回原模型或清库重建">
                <a-tag color="orange">向量停用(模型变更)</a-tag>
              </a-tooltip>
              <a-tooltip v-if="currentKb.schemaTemplate" :title="`建库所选本体模板:${currentKb.schemaTemplate}(可在「本体设置」页自定义实体/关系类型)`">
                <a-tag color="geekblue">本体:{{ currentKb.schemaTemplate }}</a-tag>
              </a-tooltip>
              <span v-if="currentKb.description" class="kg-info-desc">{{ currentKb.description }}</span>
            </div>
            <div class="kg-info-stats">
              <span><FileTextOutlined /> 文档 <b>{{ currentKb.docCount ?? 0 }}</b></span>
              <span><NodeIndexOutlined /> 实体 <b>{{ currentKb.entityTotal ?? 0 }}</b></span>
              <span><ApartmentOutlined /> 关系 <b>{{ currentKb.relationTotal ?? 0 }}</b></span>
            </div>
            <div class="kg-info-actions">
              <a-popconfirm
                title="删除当前知识库将同时清除图谱、向量与上传文件,确认?"
                ok-text="删除" ok-type="danger" @confirm="doDeleteCurrent"
              >
                <a-button size="small" danger><DeleteOutlined /> 删除</a-button>
              </a-popconfirm>
              <a-button size="small" @click="refreshAll"><SyncOutlined :spin="loadingKbs" /> 刷新</a-button>
            </div>
          </div>

          <a-tabs v-model:activeKey="tab" size="small">
            <a-tab-pane key="docs">
              <template #tab><FileTextOutlined /> 文档导入</template>
              <DocManager :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
            </a-tab-pane>
            <a-tab-pane key="graph">
              <template #tab><NodeIndexOutlined /> 图谱浏览</template>
              <GraphView :kb-id="currentKbId" :schema="currentKb?.schema || null" :refresh-tick="refreshTick" />
            </a-tab-pane>
            <a-tab-pane key="schema">
              <template #tab><ApartmentOutlined /> 本体设置</template>
              <SchemaEditor :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
            </a-tab-pane>
            <a-tab-pane key="retrieval-test">
              <template #tab><ExperimentOutlined /> 检索测试</template>
              <RetrievalTest :kb-id="currentKbId" />
            </a-tab-pane>
          </a-tabs>
        </template>

        <!-- 右侧空态:无库或未选中 -->
        <div v-else class="kg-empty">
          <PartitionOutlined class="kg-empty-icon" />
          <div class="kg-empty-title">{{ kbs.length ? '未选择知识库' : '还没有知识库' }}</div>
          <div class="kg-empty-sub">
            {{ kbs.length ? '从左侧列表选择一个知识库查看详情' : '每个库独立本体 / 图谱 / 向量 collection(物理隔离,删库即清)——点左侧「新建」创建第一个' }}
          </div>
        </div>
      </section>
    </div>

    <!-- 建库弹窗:选本体模板起步(自原库管理页并入) -->
    <a-modal v-model:open="createOpen" title="新建知识库" :confirm-loading="creating"
             ok-text="创建" cancel-text="取消" @ok="doCreate">
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
// 结构:左右分栏——左侧知识库列表(选择 + 新建,替代原顶部下拉工作条),
//       右侧选中库详情(信息条 + 删除/刷新 + 四个子页:文档导入/图谱浏览/本体设置/检索测试)。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ApartmentOutlined, DeleteOutlined, ExperimentOutlined, FileTextOutlined,
  NodeIndexOutlined, PartitionOutlined, PlusOutlined, SyncOutlined,
} from '@ant-design/icons-vue'
import {
  KgKnowledgeBase, KgTemplate, createKgKb, deleteKgKb, fetchKgKbs, fetchKgTemplates,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import DocManager from './DocManager.vue'
import SchemaEditor from './SchemaEditor.vue'
import GraphView from './GraphView.vue'
import RetrievalTest from './RetrievalTester.vue'

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

/* ── 左右分栏骨架 ─────────────────────────────── */
.kg-split { display: flex; gap: 14px; align-items: stretch; }
.kg-side {
  width: 260px; flex-shrink: 0; align-self: flex-start;
  border: 1px solid #e5e6eb; border-radius: 10px; background: #fff;
  padding: 10px; position: sticky; top: 76px;
}
.kg-main { flex: 1; min-width: 0; }

/* 左侧:标题行 + 库列表 */
.kg-side-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 2px 4px 8px; border-bottom: 1px solid #eef1f5; margin-bottom: 6px;
}
.kg-side-title { display: flex; align-items: center; gap: 6px; font-weight: 600; color: #1f2329; }
.kg-side-icon { color: #2f54eb; font-size: 15px; }
.kg-side-list { max-height: calc(100vh - 220px); overflow-y: auto; }
.kg-side-tip { padding: 16px 4px; text-align: center; color: #94a3b8; font-size: 12.5px; }

.kb-item {
  border-radius: 8px; padding: 8px 10px; cursor: pointer;
  transition: background 0.15s;
}
.kb-item:hover { background: #f2f6ff; }
.kb-item.on { background: #eaf0ff; }
.kb-item.on .kb-item-name { color: #3370ff; }
.kb-item-name {
  font-size: 13px; font-weight: 600; color: #1f2329;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.kb-item-meta { font-size: 11.5px; color: #94a3b8; margin-top: 2px; }
.kb-item-empty {
  border: 1.5px dashed #c7d2fe; border-radius: 10px; padding: 18px 10px;
  text-align: center; cursor: pointer; color: #9ca3af; font-size: 12.5px; line-height: 1.7;
  background: #fafbff; transition: border-color 0.15s, background 0.15s;
}
.kb-item-empty:hover { border-color: #2f54eb; background: #f5f7ff; color: #2f54eb; }
.kb-item-empty-icon { font-size: 16px; color: #2f54eb; margin-bottom: 4px; }

/* 右侧:信息条(含原工作条拆解出的删除/刷新) */
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
.kg-info-actions { display: flex; gap: 8px; flex-shrink: 0; }

/* 右侧空态:无库/未选中提示(创建引导在左侧列表) */
.kg-empty {
  border: 1.5px dashed #dcdfe6; border-radius: 12px; padding: 60px 20px; text-align: center;
  background: #fafbfc;
}
.kg-empty-icon { font-size: 30px; color: #c9cdd4; }
.kg-empty-title { font-weight: 700; font-size: 15px; color: #4e5969; margin-top: 8px; }
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
