<template>
  <div class="kg-page">
    <!-- 左右分栏:左侧知识库层级树(目录+子库),右侧选中库详情 -->
    <div class="kg-split">
      <!-- 左侧:知识库树(a-tree;folder=可折叠目录,kb=叶子带统计) -->
      <aside class="kg-side">
        <div class="kg-side-head">
          <div class="kg-side-title"><PartitionOutlined class="kg-side-icon" /> 知识库</div>
          <div class="kg-side-btns">
            <a-button size="small" type="primary" @click="wizardRef?.show()"><PlusOutlined /> 新建库</a-button>
            <a-tooltip title="新建文件夹(仅作分组目录)">
              <a-button size="small" @click="openFolderCreate"><FolderAddOutlined /></a-button>
            </a-tooltip>
          </div>
        </div>
        <div class="kg-side-tree">
          <div v-if="loadingKbs && !tree.length" class="kg-side-tip">加载中…</div>
          <template v-else>
            <a-tree
              v-model:expanded-keys="expandedKeys"
              :selected-keys="currentKbId ? [currentKbId] : []"
              :tree-data="treeData"
              block-node
              @select="onTreeSelect"
            >
              <template #title="{ dataRef }">
                <div class="kb-node">
                  <FolderOutlined v-if="dataRef.raw.kind === 'folder'" class="kb-node-icon folder" />
                  <PartitionOutlined v-else class="kb-node-icon kb" />
                  <div class="kb-node-label">
                    <div class="kb-node-name" :title="dataRef.raw.name">{{ dataRef.raw.name }}</div>
                    <div v-if="dataRef.raw.kind === 'kb'" class="kb-node-meta">
                      {{ dataRef.raw.docCount ?? 0 }} 文档 / {{ dataRef.raw.entityTotal ?? 0 }} 实体
                    </div>
                  </div>
                  <!-- hover 操作:kb=移动;folder=重命名/删除(非空 422 提示) -->
                  <span class="kb-node-acts" @click.stop>
                    <template v-if="dataRef.raw.kind === 'kb'">
                      <a-tooltip title="移动到…">
                        <SwapOutlined class="kb-act" @click.stop="openMove(dataRef.raw)" />
                      </a-tooltip>
                    </template>
                    <template v-else>
                      <a-tooltip title="重命名">
                        <EditOutlined class="kb-act" @click.stop="openRename(dataRef.raw)" />
                      </a-tooltip>
                      <a-popconfirm
                        title="删除该文件夹?需先清空其下子节点"
                        ok-text="删除" ok-type="danger" @confirm="doDeleteFolder(dataRef.raw)"
                      >
                        <DeleteOutlined class="kb-act danger" />
                      </a-popconfirm>
                    </template>
                  </span>
                </div>
              </template>
            </a-tree>
            <!-- 树空态:引导新建 -->
            <div v-if="!tree.length" class="kb-item-empty" @click="wizardRef?.show()">
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
              <DocManager :kb-id="currentKbId" :refresh-tick="refreshTick"
                          :chunk-params="currentKb?.chunkParams ?? null" @changed="loadKbs" />
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
            <a-tab-pane key="acl">
              <template #tab>
                <span class="kg-acl-tab">
                  <SafetyOutlined /> 数据权限
                  <a-popover placement="bottom" trigger="click">
                    <template #content>
                      <div class="kg-acl-tip">
                        <p>仅当宿主传入用户身份(X-User-Id)或角色(pack_params 的 user_roles)时生效;</p>
                        <p><b>不配置 = 所有人可检索</b>;未传身份时不过滤。</p>
                        <p>用户/角色候选由宿主经同步接口写入;管理端页面不受此限制。</p>
                      </div>
                    </template>
                    <a class="kg-acl-info" @click.stop><QuestionCircleOutlined /></a>
                  </a-popover>
                </span>
              </template>
              <AclEditor :kb-id="currentKbId" />
            </a-tab-pane>
          </a-tabs>
        </template>

        <!-- 右侧空态:无库或未选中 -->
        <div v-else class="kg-empty">
          <PartitionOutlined class="kg-empty-icon" />
          <div class="kg-empty-title">{{ kbs.length ? '未选择知识库' : '还没有知识库' }}</div>
          <div class="kg-empty-sub">
            {{ kbs.length ? '从左侧树选择一个知识库查看详情' : '每个库独立本体 / 图谱 / 向量 collection(物理隔离,删库即清)——点左侧「新建库」创建第一个' }}
          </div>
        </div>
      </section>
    </div>

    <!-- D33 建库三步向导:基本信息+上传 → 分段配置+实时预览 → 确认导入
         (替换原"新建库"简单弹窗——分段参数从插件设置里挪到建库时可视化调参) -->
    <KbWizard ref="wizardRef" :templates="templates" :parent-options="parentOptions"
              @created="onWizardCreated" />

    <!-- 新建文件夹弹窗:简单输入(纯分组目录,不存文档) -->
    <a-modal v-model:open="folderOpen" title="新建文件夹" :confirm-loading="folderCreating"
             ok-text="创建" cancel-text="取消" @ok="doFolderCreate">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="文件夹名称" required>
          <a-input v-model:value="folderName" placeholder="如:法务、产品线" :maxlength="60" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 移动弹窗:选目标目录/顶层 -->
    <a-modal v-model:open="moveOpen" :title="`移动「${moveTarget?.name ?? ''}」`" :confirm-loading="moving"
             ok-text="移动" cancel-text="取消" @ok="doMove">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="目标位置">
          <a-select v-model:value="moveParentId" :options="moveOptions" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 文件夹重命名弹窗 -->
    <a-modal v-model:open="renameOpen" title="重命名文件夹" :confirm-loading="renaming"
             ok-text="保存" cancel-text="取消" @ok="doRename">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="文件夹名称" required>
          <a-input v-model:value="renameName" :maxlength="60" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 知识图谱管理页(manifest admin.page=knowledge_graph → AdminApp 动态 Tab 挂载)。
// 结构:左右分栏——左侧知识库层级树(a-tree:目录可折叠+子库叶子,新建库/文件夹、
//       移动/重命名/删除),右侧选中库详情(信息条 + 四个子页)。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import type { TreeDataItem } from 'ant-design-vue/es/tree'
import {
  ApartmentOutlined, DeleteOutlined, EditOutlined, ExperimentOutlined,
  FileTextOutlined, FolderAddOutlined, FolderOutlined, NodeIndexOutlined,
  PartitionOutlined, PlusOutlined, QuestionCircleOutlined, SafetyOutlined,
  SwapOutlined, SyncOutlined,
} from '@ant-design/icons-vue'
import {
  KgKnowledgeBase, KgTemplate, KgTreeNode, createKgFolder, deleteKgKb,
  fetchKgKbTree, fetchKgTemplates, moveKbNode, updateKgKb,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import {
  buildFolderOptions, collectFolderKeys, flattenKbs, mapTreeData, subtreeIds,
} from './kbTree'
import DocManager from './DocManager.vue'
import SchemaEditor from './SchemaEditor.vue'
import GraphView from './GraphView.vue'
import RetrievalTest from './RetrievalTester.vue'
import AclEditor from './AclEditor.vue'
import KbWizard from './KbWizard.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

// ── 左侧树状态 ────────────────────────────────────────
const tree = ref<KgTreeNode[]>([])
const currentKbId = ref('')
const loadingKbs = ref(false)
const tab = ref('docs')
// 刷新联动:点「刷新」时自增,子页(文档/图谱/本体)watch 它重载
const refreshTick = ref(0)
// 树展开键(默认展开全部:每次加载后置为全部目录键)
const expandedKeys = ref<(string | number)[]>([])

// kb-only 平铺(右侧详情数据源;folder 不进详情)
const kbs = computed<KgKnowledgeBase[]>(() => flattenKbs(tree.value))

const currentKb = computed(() => kbs.value.find((k) => k.id === currentKbId.value))

// a-tree 数据:folder=可折叠父节点(不可选中详情),kb=叶子(映射逻辑见 kbTree.ts)
const treeData = computed<TreeDataItem[]>(() => mapTreeData(tree.value))

// 目录选项(建库父目录下拉/移动目标):带路径前缀便于区分同名
const folderOptions = computed(() => buildFolderOptions(tree.value))

function refreshAll() {
  refreshTick.value++
  loadKbs()
}

let loadKbsSeq = 0   // 请求序号守卫:旧响应晚到不得清掉刚选中的库(如新建后)

async function loadKbs() {
  const seq = ++loadKbsSeq
  loadingKbs.value = true
  await loadSafely(async () => {
    const items = await fetchKgKbTree()
    if (seq !== loadKbsSeq) return   // 已有更新请求,丢弃过期结果
    tree.value = items
    // 默认展开全部(10-15 需求)
    expandedKeys.value = collectFolderKeys(items)
    if (currentKbId.value && !kbs.value.some((k) => k.id === currentKbId.value)) {
      currentKbId.value = ''
    }
    if (!currentKbId.value && kbs.value.length === 1) {
      currentKbId.value = kbs.value[0].id
    }
  })
  if (seq === loadKbsSeq) loadingKbs.value = false
}

// 树选择:folder 不可选中(mapTreeData selectable=false),仅 kb 触发
function onTreeSelect(keys: (string | number)[]) {
  currentKbId.value = keys.length ? String(keys[0]) : ''
}

// ── 建库(D33 三步向导) ──────────────────────────────
const templates = ref<KgTemplate[]>([])
const wizardRef = ref<InstanceType<typeof KbWizard> | null>(null)

const parentOptions = computed(() => [
  { value: '', label: '（顶层）' },
  ...folderOptions.value.map((f) => ({ value: f.id, label: f.path })),
])

function onWizardCreated(kbId: string) {
  currentKbId.value = kbId
  tab.value = 'docs'
  loadKbs()
}

onMounted(async () => {
  loadKbs()
  await loadSafely(async () => {
    templates.value = await fetchKgTemplates()
  })
})

// ── 新建文件夹(简单输入弹窗) ──────────────────────
const folderOpen = ref(false)
const folderCreating = ref(false)
const folderName = ref('')

function openFolderCreate() {
  folderName.value = ''
  folderOpen.value = true
}

async function doFolderCreate() {
  if (!folderName.value.trim()) {
    message.warning('请输入文件夹名称')
    return
  }
  folderCreating.value = true
  await loadSafely(async () => {
    const f = await createKgFolder(folderName.value.trim())
    message.success(`文件夹「${f.name}」已创建`)
    folderOpen.value = false
    loadKbs()
  })
  folderCreating.value = false
}

// ── 移动节点(换父目录/提顶层) ──────────────────────
const moveOpen = ref(false)
const moving = ref(false)
const moveTarget = ref<KgTreeNode | null>(null)
const moveParentId = ref('')

// 移动目标:folder 移动时排除自身子树(移进去成环,后端也会 422)
const moveOptions = computed(() => {
  const banned = moveTarget.value && moveTarget.value.kind === 'folder'
    ? subtreeIds(moveTarget.value)
    : new Set<string>()
  return [
    { value: '', label: '（顶层）' },
    ...folderOptions.value.filter((f) => !banned.has(f.id))
      .map((f) => ({ value: f.id, label: f.path })),
  ]
})

function openMove(node: KgTreeNode) {
  moveTarget.value = node
  moveParentId.value = node.parentId ?? ''
  moveOpen.value = true
}

async function doMove() {
  const node = moveTarget.value
  if (!node) return
  moving.value = true
  await loadSafely(async () => {
    await moveKbNode(node.id, moveParentId.value || null)
    message.success(`已移动「${node.name}」`)
    moveOpen.value = false
    loadKbs()
  })
  moving.value = false
}

// ── 文件夹重命名/删除 ──────────────────────────────
const renameOpen = ref(false)
const renaming = ref(false)
const renameTarget = ref<KgTreeNode | null>(null)
const renameName = ref('')

function openRename(node: KgTreeNode) {
  renameTarget.value = node
  renameName.value = node.name
  renameOpen.value = true
}

async function doRename() {
  const node = renameTarget.value
  if (!node) return
  if (!renameName.value.trim()) {
    message.warning('请输入文件夹名称')
    return
  }
  renaming.value = true
  await loadSafely(async () => {
    await updateKgKb(node.id, { name: renameName.value.trim() })
    message.success('文件夹已重命名')
    renameOpen.value = false
    loadKbs()
  })
  renaming.value = false
}

async function doDeleteFolder(node: KgTreeNode) {
  await loadSafely(async () => {
    // 非空目录后端 422(子节点会成孤儿),文案经 loadSafely toast 透出
    await deleteKgKb(node.id)
    message.success(`文件夹「${node.name}」已删除`)
    loadKbs()
  })
}

// ── 删库(右侧信息条) ──────────────────────────────
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
.kg-page { padding: 0; }

/* ── 左右分栏骨架 ─────────────────────────────── */
.kg-split { display: flex; gap: 12px; align-items: stretch; }
.kg-side {
  width: 232px; flex-shrink: 0; align-self: flex-start;
  border: 1px solid #e5e6eb; border-radius: 10px; background: #fff;
  padding: 8px; position: sticky; top: 76px;
}
.kg-main { flex: 1; min-width: 0; }

/* 左侧:标题行 + 树 */
.kg-side-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 2px 4px 8px; border-bottom: 1px solid #eef1f5; margin-bottom: 6px;
}
.kg-side-title { display: flex; align-items: center; gap: 6px; font-weight: 600; color: #1f2329; }
.kg-side-icon { color: #2f54eb; font-size: 15px; }
.kg-side-btns { display: flex; gap: 6px; }
.kg-side-tree { max-height: calc(100vh - 220px); overflow-y: auto; }
.kg-side-tree :deep(.ant-tree) { background: transparent; font-size: 13px; }
.kg-side-tree :deep(.ant-tree .ant-tree-node-content-wrapper) {
  padding: 0 2px; border-radius: 6px;
}
/* 顶层节点贴左(用户 10-15 报"顶层缩紧不合适"):只清顶层的 indent
   单元与 treenode 左内边距;子层保持层级缩进(层级感不丢) */
.kg-side-tree :deep(.ant-tree > .ant-tree-list-holder-inner > div > .ant-tree-treenode) {
  padding-left: 0 !important;
}
.kg-side-tree :deep(
  .ant-tree > .ant-tree-list-holder-inner > div > .ant-tree-treenode
  > .ant-tree-switcher) {
  margin-left: 0;
}
.kg-side-tree :deep(.ant-tree .ant-tree-switcher) { width: 18px; }
.kg-side-tip { padding: 16px 4px; text-align: center; color: #94a3b8; font-size: 12.5px; }

/* 树节点:图标 + 名称/统计 + hover 操作 */
.kb-node {
  display: flex; align-items: center; gap: 6px;
  width: 100%; min-width: 0; padding: 1px 0;
}
.kb-node-icon { font-size: 14px; flex-shrink: 0; }
.kb-node-icon.folder { color: #faad14; }
.kb-node-icon.kb { color: #2f54eb; }
.kb-node-label { flex: 1; min-width: 0; }
.kb-node-name {
  font-size: 13px; font-weight: 600; color: #1f2329;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.kb-node-meta { font-size: 11px; color: #94a3b8; margin-top: 1px; line-height: 1.2; }
.kb-node-acts { display: none; align-items: center; gap: 6px; flex-shrink: 0; }
.kb-node:hover .kb-node-acts { display: inline-flex; }
.kb-act { font-size: 13px; color: #6b7280; cursor: pointer; }
.kb-act:hover { color: #2f54eb; }
.kb-act.danger:hover { color: #ef4444; }

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

/* 右侧空态:无库/未选中提示(创建引导在左侧树) */
.kg-empty {
  border: 1.5px dashed #dcdfe6; border-radius: 12px; padding: 60px 20px; text-align: center;
  background: #fafbfc;
}
.kg-empty-icon { font-size: 30px; color: #c9cdd4; }
.kg-acl-tab { display: inline-flex; align-items: center; gap: 4px; }
.kg-acl-info { color: #94a3b8; display: inline-flex; align-items: center; }
.kg-acl-info:hover { color: #2f54eb; }
.kg-acl-tip { max-width: 320px; font-size: 12.5px; line-height: 1.8; color: #4b5563; }
.kg-acl-tip p { margin: 0; }
.kg-empty-title { font-weight: 700; font-size: 15px; color: #4e5969; margin-top: 8px; }
.kg-empty-sub { font-size: 12.5px; color: #9ca3af; margin-top: 4px; }
</style>
