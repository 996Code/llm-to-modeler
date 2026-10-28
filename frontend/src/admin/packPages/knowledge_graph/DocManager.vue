<template>
  <div>
    <!-- 上传区 -->
    <a-upload-dragger
      :file-list="[]" :multiple="true" :show-upload-list="false"
      :before-upload="() => false"
      accept=".md,.markdown,.txt,.pdf,.docx"
      @change="onFilesPicked"
    >
      <p class="dm-upload-icon"><InboxOutlined /></p>
      <p class="dm-upload-text">点击或拖拽文件到此处上传</p>
      <p class="dm-upload-hint">
        支持 md / txt / pdf / docx，单文件 ≤ {{ maxMb }}MB；同内容自动去重，扫描版 PDF 自动识别
        <a-popover placement="bottom" trigger="hover">
          <template #content>
            <div class="dm-chunk-pop">
              <div class="dm-chunk-pop-title">导入如何切块(结构感知)</div>
              <ul class="dm-chunk-list">
                <li><b>结构块</b>:识别到章节标题(如"第X章/第X节")时整章一块,保持上下文完整(超长章在句读处兜底再切)</li>
                <li><b>无结构文本</b>:按句读边界软目标 {{ chunkTargetChars }} 字/块切,保持语义连贯</li>
                <li><b>向量子切</b>:整章大块向量化前再按 {{ vectorSubChars }} 字句读切段——抽取看整章(上下文全),检索召回按段(向量不稀释)</li>
                <li><b>父子回带</b>:检索用小段匹配(语义集中命中准),命中后回带该段在<b>所属整章</b>中前后各约 2000 字的上下文(句读对齐)发给 AI——AI 看到的是连贯上下文,不是碎片;「检索测试」页可看每段回带明细</li>
                <li>每块的抽取状态可点行内「块明细」查看;块大小可在<b>插件设置</b>调整</li>
              </ul>
            </div>
          </template>
          <a class="dm-chunk-link" @click.stop @mousemove.stop><QuestionCircleOutlined /> 切块规则</a>
        </a-popover>
      </p>
    </a-upload-dragger>

    <!-- 上传结果反馈 -->
    <a-alert
      v-if="uploadResults.length" type="info" closable show-icon style="margin-top: 10px"
      :message="`本次上传:${uploadResults.filter((r) => r.ok).length} 成功 / ${uploadResults.filter((r) => !r.ok).length} 跳过`"
    >
      <template #description>
        <div v-for="(r, i) in uploadResults" :key="i" class="dm-upload-line">
          <CheckCircleOutlined v-if="r.ok" class="dm-ok" />
          <CloseCircleOutlined v-else class="dm-bad" />
          {{ r.filename }}<span v-if="!r.ok" class="dm-reason"> —— {{ r.reason }}</span>
        </div>
      </template>
    </a-alert>

    <!-- 工具条(一行):导入 / 分段 / 批量删除 / 统计 / 提示 -->
    <div class="dm-toolbar">
      <a-button type="primary" size="small" :disabled="!importableCount" :loading="importingAll" @click="doImportAll">
        <CloudUploadOutlined /> 导入全部未处理文档
      </a-button>
      <!-- 分段配置:已有文档(解析开始)后分段口径不可再改(后端同款 422 守卫) -->
      <a-tooltip v-if="docs.length" title="该库已有文档导入,分段参数不可修改">
        <a-button size="small" disabled><SettingOutlined /> 分段配置</a-button>
      </a-tooltip>
      <a-button v-else size="small" @click="openChunkConfig"><SettingOutlined /> 分段配置</a-button>
      <a-popconfirm
        :title="`将删除选中的 ${selectedIds.length} 个文档(含图谱贡献与向量),确认?`"
        :disabled="!selectedIds.length" @confirm="doBatchDelete"
      >
        <a-button danger size="small" :disabled="!selectedIds.length" :loading="batchDeleting">
          <DeleteOutlined /> 批量删除{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
        </a-button>
      </a-popconfirm>
      <a-button size="small" @click="loadUsage">
        <BarChartOutlined /> 使用统计
      </a-button>
      <!-- 状态筛选:文档表是解析状态真相源(任务页只反映任务执行记录,
           中断自动重排的任务是 pending/cancelled 而非 failed) -->
      <a-select v-model:value="statusFilter" size="small" style="width: 150px"
                @change="onStatusFilterChange">
        <a-select-option value="">全部状态 ({{ docs.length }})</a-select-option>
        <a-select-option v-for="(meta, s) in STATUS" :key="s" :value="s">
          {{ meta.label }} ({{ statusCounts[s] || 0 }})
        </a-select-option>
      </a-select>
      <span class="dm-tip">上传成功后自动导入，可在任务中心查看进度</span>
    </div>

    <!-- D33 分段重配(已有库):调参与预览同向导;保存为库级参数,重导入生效 -->
    <a-modal v-model:open="chunkConfigOpen" title="分段配置(重导入后生效)" :width="880"
             :confirm-loading="chunkConfigSaving" ok-text="保存" cancel-text="取消" @ok="saveChunkConfig">
      <ChunkConfigPanel v-model="chunkParams" :files="[]" />
      <a-alert type="warning" show-icon style="margin-top: 10px"
               message="保存仅影响后续导入;已导入文档需「重导」才会按新分段重建(图谱实体随重导同步重建)" />
    </a-modal>

    <!-- 文档表 -->
    <a-table
      :columns="columns" :data-source="filteredDocs" :loading="loading" row-key="id"
      :row-selection="{ selectedRowKeys: selectedIds, onChange: (k: string[]) => selectedIds = k }"
      size="small" :scroll="{ x: 1100 }" :pagination="{ pageSize: 20, showTotal: (t: number) => `共 ${t} 条` }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'filename'">
          <FileTextOutlined class="dm-file-icon" :class="extOf(record.filename)" />
          <span class="dm-fname">{{ record.filename }}</span>
        </template>
        <template v-else-if="column.key === 'usage'">
          <!-- D30 使用统计:命中总量(D29 数据);未加载时显示加载入口 -->
          <span v-if="usageById[record.id] !== undefined" class="dm-mono">
            {{ usageById[record.id] }} 次引用
          </span>
          <a v-else class="dm-meta-add" @click="loadUsage">查看</a>
        </template>
        <template v-else-if="column.key === 'metadata'">
          <template v-if="metaEntries(record).length">
            <a-tag v-for="[k, v] in metaEntries(record)" :key="k" class="dm-meta-tag"
                   @click="openMetaEdit(record)">{{ k }}:{{ v }}</a-tag>
          </template>
          <a v-else class="dm-meta-add" @click="openMetaEdit(record)">+ 打标</a>
        </template>
        <template v-else-if="column.key === 'size'">
          <span class="dm-mono">{{ fmtSize(record.sizeBytes) }}</span>
        </template>
        <template v-else-if="column.key === 'importStatus'">
          <a-tooltip v-if="record.error" :title="record.error">
            <a-tag :color="statusColor(record.importStatus)">{{ statusLabel(record.importStatus) }} !</a-tag>
          </a-tooltip>
          <a-tag v-else :color="statusColor(record.importStatus)">{{ statusLabel(record.importStatus) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'counts'">
          <span class="dm-counts">
            {{ record.chunkCount }} 块 / {{ record.entityCount }} 实体 / {{ record.relationCount }} 关系
          </span>
        </template>
        <template v-else-if="column.key === 'progress'">
          <a-progress
            v-if="taskByDoc[record.id] && !isFinal(taskByDoc[record.id])"
            :percent="taskByDoc[record.id].progress" size="small" status="active"
          />
          <span v-else-if="taskByDoc[record.id]?.status === 'succeeded'" class="dm-done">完成</span>
          <span v-else>-</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="14">
            <a-tooltip v-if="record.importStatus !== 'importing'"
                       :title="record.importStatus === 'succeeded' ? '重新导入' : '导入'">
              <a @click="doImport(record, false)"><CloudUploadOutlined /></a>
            </a-tooltip>
            <a-tooltip title="强制重导:忽略已有进度与内容判断,全部重抽(本体变更后用)">
              <a @click="doImport(record, true)"><RedoOutlined /></a>
            </a-tooltip>
            <a-tooltip title="块明细:逐块查看导入状态,定向重试失败块">
              <a @click="openChunks(record)"><UnorderedListOutlined /></a>
            </a-tooltip>
            <a-tooltip title="下载已上传的原始文件">
              <a @click="doDownload(record)"><DownloadOutlined /></a>
            </a-tooltip>
            <a-popconfirm title="删除文档将清除其图谱贡献与向量,确认?" @confirm="doDelete(record)">
              <a class="dm-danger"><DeleteOutlined /></a>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <!-- 块明细抽屉:逐块导入状态 + 定向重试(批次是执行时的动态分组,持久状态在块级) -->
    <!-- :push=false 关闭嵌套抽屉推挤——内部"抽取产出"抽屉打开时,本抽屉
         会被 antd 默认向左推 180px,表现为"块明细位置突变" -->
    <a-drawer v-model:open="chunksDrawer.open" width="720" :push="false"
              :title="`块明细 · ${chunksDrawer.doc?.filename || ''}`">
      <div v-if="chunksDrawer.payload" class="dm-chunks">
        <div class="dm-chunks__summary">
          <a-tag>共 {{ chunksDrawer.payload.summary.total }} 块</a-tag>
          <a-tag color="green">完成 {{ chunksDrawer.payload.summary.done }}</a-tag>
          <a-tag color="red">失败 {{ chunksDrawer.payload.summary.failed }}</a-tag>
          <a-tag color="orange">待处理 {{ chunksDrawer.payload.summary.pending }}</a-tag>
          <span class="dm-chunks__spacer" />
          <a-button
            size="small" type="primary" ghost
            :disabled="!chunksDrawer.payload.summary.failed"
            @click="retryFailedChunks()"
          ><RedoOutlined /> 重试全部失败块</a-button>
          <a-button size="small" @click="openChunks(chunksDrawer.doc!)"><template #icon><ReloadOutlined /></template>刷新</a-button>
        </div>
        <a-table
          :columns="chunkColumns" :data-source="chunksDrawer.payload.items"
          :loading="chunksDrawer.loading" row-key="id" size="small"
          :pagination="{ pageSize: 50, showTotal: (t: number) => `共 ${t} 块` }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'seq'">
              <span class="dm-mono">#{{ record.seq }}</span>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tag :color="chunkStatusColor(record.status)" class="dm-chunks__st">
                {{ chunkStatusLabel(record.status) }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'charCount'">
              <span class="dm-mono">{{ record.charCount }}</span>
            </template>
            <template v-else-if="column.key === 'hitCount'">
              <a-tag v-if="record.hitCount > 0" color="blue" class="dm-mono">
                {{ record.hitCount }} 次引用
              </a-tag>
              <span v-else class="dm-chunks__dim">—</span>
            </template>
            <template v-else-if="column.key === 'preview'">
              <span class="dm-chunks__preview" :title="record.preview">{{ record.preview }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <!-- 行内按钮图标化:文字进悬浮提示,不占列宽(操作列规约) -->
              <a-tooltip title="重试该块(只补此块,已完成块跳过)">
                <a v-if="record.status !== 'done'" @click="retryOneChunk(record)">
                  <RedoOutlined />
                </a>
              </a-tooltip>
              <a-tooltip title="查看抽取产出(实体/关系)">
                <a v-if="record.status === 'done'" @click="showExtraction(record)">
                  <EyeOutlined />
                </a>
              </a-tooltip>
              <a-tooltip title="编辑块文本(仅重建该块向量,图谱不随编辑更新)">
                <a @click="openChunkEdit(record)">
                  <EditOutlined />
                </a>
              </a-tooltip>
              <span v-if="record.status !== 'done'" class="dm-chunks__dim">—</span>
            </template>
          </template>
        </a-table>
        <p class="dm-chunks__hint">
          重试只补选中块(已完成块跳过,图谱不受影响);失败块也会在整文档重导时自动续跑。
          <b>编辑</b>改文本后仅重建该块向量(未编辑块零重算);<b>图谱实体不随编辑更新</b>——
          该块抽出的实体/关系仍是旧文本的,需「重导」全量重建。
        </p>

        <!-- D7 元数据编辑:键值对(检索过滤维度) -->
        <a-modal v-model:open="metaEdit.open" :title="`元数据 · ${metaEdit.filename}`"
                 :confirm-loading="metaEdit.saving" ok-text="保存" cancel-text="取消" @ok="saveMeta">
          <p class="dm-meta-hint">扁平键值(如 部门=销售);检索时宿主传 doc_filter 按此过滤,留空键的行忽略</p>
          <div v-for="(row, i) in metaEdit.rows" :key="i" class="dm-meta-row">
            <a-input v-model:value="row.key" placeholder="键(如 部门)" style="width: 35%" :maxlength="64" />
            <a-input v-model:value="row.value" placeholder="值(如 销售;逗号分隔多值)" style="width: 50%"
                     :maxlength="128" @pressEnter="saveMeta" />
            <a-tooltip title="删除该行">
                <a-button size="small" type="text" danger @click="metaEdit.rows.splice(i, 1)"><template #icon><DeleteOutlined /></template></a-button>
              </a-tooltip>
          </div>
          <a-button size="small" type="dashed" block style="margin-top: 8px"
                    @click="metaEdit.rows.push({ key: '', value: '' })">+ 添加</a-button>
        </a-modal>

        <!-- D27 块编辑:改文本 → 仅重嵌该块向量 -->
        <a-modal v-model:open="chunkEdit.open" :title="`编辑块 #${chunkEdit.seq}`"
                 :confirm-loading="chunkEdit.saving" ok-text="保存并重建向量" cancel-text="取消"
                 @ok="saveChunkEdit">
          <a-alert type="warning" show-icon style="margin-bottom: 10px"
                   message="保存后仅重建该块向量与全文索引;图谱实体需整文档重导才会同步" />
          <a-textarea v-model:value="chunkEdit.text" :rows="12" :maxlength="50000"
                      show-count placeholder="块文本" />
          <div v-if="chunkEdit.warning" class="dm-chunk-edit-warn">{{ chunkEdit.warning }}</div>
        </a-modal>

        <!-- 单块产出抽屉:该块实际抽出的实体与关系(块级溯源) -->
        <a-drawer
          v-model:open="extractionDrawer.open" width="560" placement="left"
          :title="`块 #${extractionDrawer.seq} 的抽取产出`"
        >
          <div v-if="extractionDrawer.loading">加载中…</div>
          <div v-else-if="extractionDrawer.data" class="dm-ext">
            <div class="dm-ext__section">
              <div class="dm-ext__title">实体({{ extractionDrawer.data.entities.length }})</div>
              <div v-if="!extractionDrawer.data.entities.length" class="dm-chunks__dim">
                无实体(内容被锚定过滤/目录块)
              </div>
              <div v-for="(e, i) in extractionDrawer.data.entities" :key="i" class="dm-ext__row">
                <a-tag color="blue">{{ e.type || '?' }}</a-tag>
                <span class="dm-ext__name">{{ e.name }}</span>
                <span class="dm-ext__desc">{{ e.description }}</span>
              </div>
            </div>
            <div class="dm-ext__section">
              <div class="dm-ext__title">关系({{ extractionDrawer.data.relations.length }})</div>
              <div v-if="!extractionDrawer.data.relations.length" class="dm-chunks__dim">无关系</div>
              <div v-for="(r, i) in extractionDrawer.data.relations" :key="i" class="dm-ext__row">
                <code class="dm-ext__rel">{{ r.source }} →[{{ r.type }}]→ {{ r.target }}</code>
                <div v-if="r.evidence" class="dm-ext__evidence">证据:{{ r.evidence }}</div>
              </div>
            </div>
          </div>
        </a-drawer>
      </div>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 文档管理:拖拽上传(查重/预检) + 导入任务发起 + 行内任务进度轮询。
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { UploadChangeParam } from 'ant-design-vue'
import {BarChartOutlined, CheckCircleOutlined, CloseCircleOutlined, CloudUploadOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, EyeOutlined, FileTextOutlined, InboxOutlined, QuestionCircleOutlined, RedoOutlined, ReloadOutlined, SettingOutlined, UnorderedListOutlined} from '@ant-design/icons-vue'
import {
  KgChunkParams, KgDocument, TaskItem, batchDeleteKgDocuments, deleteKgDocument,
  downloadKgDocument, fetchKgChunkExtraction, fetchKgChunkText, fetchKgChunks, fetchKgDocuments,
  fetchKgUsage, fetchTask, importKgAll, importKgDocument, retryKgChunks,
  setKgDocMetadata, updateKgChunk, updateKgKb, uploadKgDocuments,
} from '../../api'
import type { KgChunkExtraction, KgChunkItem, KgChunksPayload } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import ChunkConfigPanel from './ChunkConfigPanel.vue'

const props = defineProps<{ kbId: string; refreshTick?: number; chunkParams?: KgChunkParams | null }>()
const emit = defineEmits<{ (e: 'changed'): void }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const docs = ref<KgDocument[]>([])
const loading = ref(false)
const uploading = ref(false)
const importingAll = ref(false)
const uploadResults = ref<{ filename: string; ok: boolean; reason?: string; document?: KgDocument }[]>([])
const taskByDoc = ref<Record<string, TaskItem>>({})
const maxMb = 500
// 切块参数展示值(与后端默认一致;实际以插件设置/库级参数为准)
const chunkTargetChars = 1200
const vectorSubChars = 500
let loadSeq = 0   // 请求序号守卫(切库错序防护)

// ── D33 分段重配(已有库):库级参数,保存后重导入生效 ──
const chunkConfigOpen = ref(false)
const chunkConfigSaving = ref(false)
const chunkParams = ref<Required<KgChunkParams>>({
  chunk_target_chars: 1200, chunk_overlap_chars: 100, chunk_max_chars: 3000,
  chunk_structural_max_chars: 10000, vector_subchunk_chars: 500,
})

function openChunkConfig() {
  // 库级参数优先,未配置项回默认(与后端解析链同口径)
  const cp = props.chunkParams || {}
  chunkParams.value = {
    chunk_target_chars: cp.chunk_target_chars ?? 1200,
    chunk_overlap_chars: cp.chunk_overlap_chars ?? 100,
    chunk_max_chars: cp.chunk_max_chars ?? 3000,
    chunk_structural_max_chars: cp.chunk_structural_max_chars ?? 10000,
    vector_subchunk_chars: cp.vector_subchunk_chars ?? 500,
  }
  chunkConfigOpen.value = true
}

// ── D7 元数据打标(检索过滤维度) ──
const metaEdit = ref<{ open: boolean; docId: string; filename: string;
                       rows: { key: string; value: string }[]; saving: boolean }>(
  { open: false, docId: '', filename: '', rows: [], saving: false })

function metaEntries(d: KgDocument): [string, string][] {
  const md = d.docMetadata as Record<string, unknown> | undefined
  if (!md) return []
  return Object.entries(md).map(([k, v]) =>
    [k, Array.isArray(v) ? v.join(',') : String(v)])
}

function openMetaEdit(d: KgDocument) {
  metaEdit.value = {
    open: true, docId: d.id, filename: d.filename, saving: false,
    rows: metaEntries(d).map(([k, v]) => ({ key: k, value: v })),
  }
}

async function saveMeta() {
  const m = metaEdit.value
  const metadata: Record<string, string | string[]> = {}
  for (const r of m.rows) {
    const k = r.key.trim()
    if (!k || !r.value.trim()) continue
    metadata[k] = r.value.includes(',') ? r.value.split(',').map(s => s.trim()).filter(Boolean) : r.value.trim()
  }
  m.saving = true
  try {
    await setKgDocMetadata(props.kbId, m.docId, metadata)
    message.success('元数据已保存')
    m.open = false
    await load()
  } catch (e) {
    message.error((e as any)?.response?.data?.detail || '保存失败')
  } finally { m.saving = false }
}

async function saveChunkConfig() {
  chunkConfigSaving.value = true
  try {
    await updateKgKb(props.kbId, { chunk_params: { ...chunkParams.value } })
    message.success('分段配置已保存(重导入文档后生效)')
    chunkConfigOpen.value = false
    emit('changed')
  } catch (e) {
    message.error((e as any)?.response?.data?.detail || '保存失败')
  } finally { chunkConfigSaving.value = false }
}

const columns = [
  { title: '文件', key: 'filename', ellipsis: true },
  { title: '元数据', key: 'metadata', width: 150 },
  { title: '大小', key: 'size', width: 90 },
  { title: '导入状态', key: 'importStatus', width: 110 },
  { title: '统计', key: 'counts', width: 190 },
  { title: '引用', key: 'usage', width: 90 },
  { title: '任务进度', key: 'progress', width: 150 },
  { title: '操作', key: 'actions', width: 170 },
]

const importableCount = computed(
  () => docs.value.filter((d) => d.importStatus !== 'succeeded').length)

const STATUS: Record<string, { color: string; label: string }> = {
  uploaded: { color: 'default', label: '待导入' },
  importing: { color: 'processing', label: '导入中' },
  succeeded: { color: 'success', label: '已导入' },
  partial: { color: 'warning', label: '部分成功' },
  failed: { color: 'error', label: '失败' },
}
const statusColor = (s: string) => STATUS[s]?.color || 'default'
const statusLabel = (s: string) => STATUS[s]?.label || s

// ── 状态筛选(本地过滤;文档列表全量拉回,千级无压力) ──
const statusFilter = ref('')
const statusCounts = computed(() => {
  const c: Record<string, number> = {}
  for (const d of docs.value) c[d.importStatus] = (c[d.importStatus] || 0) + 1
  return c
})
const filteredDocs = computed(() =>
  statusFilter.value ? docs.value.filter((d) => d.importStatus === statusFilter.value) : docs.value)
function onStatusFilterChange() {
  // 清空勾选:勾选里可能混着被筛掉的行,保留会导致"批量删除(N)"与可见行对不上
  selectedIds.value = []
}
const isFinal = (t: TaskItem) => ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(t.status)

function fmtSize(bytes: number): string {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)}MB` : `${Math.max(1, bytes / 1024) | 0}KB`
}
function extOf(name: string): string {
  return (name.split('.').pop() || '').toLowerCase()
}

async function load() {
  // 请求序号守卫:快速切换知识库时,旧库的晚到响应不得覆盖新库数据
  const myKb = props.kbId
  const seq = ++loadSeq
  loading.value = true
  await loadSafely(async () => {
    const items = await fetchKgDocuments(myKb)
    if (seq !== loadSeq || props.kbId !== myKb) return
    docs.value = items
  })
  if (seq === loadSeq) loading.value = false
}

// antd Upload 对每个文件各触发一次 onChange,且每次回调里的 fileList 都是
// "累积全量"——不防护的话选 N 个文件会以递增子集重复上传(共 N×N 份)。
// 用"文件清单签名"去重:同一批文件只在签名变化时上传一次。
let lastUploadKey = ''

async function onFilesPicked(info: UploadChangeParam) {
  const files = (info.fileList || []).map((f) => f.originFileObj).filter(Boolean) as File[]
  if (!files.length) return
  if (uploading.value) return
  const key = files.map((f) => `${f.name}:${f.size}:${f.lastModified}`).sort().join('|')
  if (key === lastUploadKey) return
  lastUploadKey = key
  uploading.value = true
  try {
    await loadSafely(async () => {
      uploadResults.value = await uploadKgDocuments(props.kbId, files)
      const okItems = uploadResults.value.filter(
        (r): r is { filename: string; ok: boolean; document: KgDocument } =>
          Boolean(r.ok && r.document?.id))
      if (okItems.length) {
        // 上传即导入:成功落库的文档直接提交后台导入任务,不再要求
        // 用户手动点"导入"(两步割裂,曾出现估时弹框吓退用户零任务)
        const tasks = await Promise.all(
          okItems.map((r) => importKgDocument(props.kbId, r.document.id, false)))
        tasks.forEach((t) => {
          const docId = (t.payload as { doc_id?: string } | null)?.doc_id
          if (docId) taskByDoc.value[docId] = t
        })
        message.success(
          `${okItems.length} 个文件已上传,导入任务已提交(后台处理,进度见任务中心)`)
      }
    })
  } finally {
    uploading.value = false
    // 签名无论成败都要复位:失败(如网关 413/网络错误)后重选同一批文件
    // 必须能重试——不复位会命中上面的签名去重被静默吞掉,表现为
    // "报错之后再上传没反应"。多文件并发去重由 uploading 标志兜底
    // (antd 同批选择的事件在上传期间同步触发,全部被 in-flight 挡住)。
    lastUploadKey = ''
  }
  await load()
  emit('changed')
}

/** 导入预估:按文档大小估块数与耗时(1 块≈1200 字,LLM 单块约 45s)。
 * 大文档(>30 分钟)先弹确认——用户上传 100 万字小说前心里有数。 */
function importEstimate(doc: KgDocument): { chunks: number; etaMin: number; etaText: string } {
  // sizeBytes 对中文 UTF-8 ≈ 3 字节/字;已切块的用真实块数
  const estChars = doc.chunkCount ? doc.chunkCount * 1200 : Math.ceil((doc.sizeBytes || 0) / 3)
  const chunks = Math.max(1, Math.ceil(estChars / 1200))
  // 45s/块 ÷ 批内并发 2 ÷ 60 → 分钟(此前漏除 60,把秒当分钟,87KB 文档
  // 被估成"9.4 小时"吓得用户取消导入)
  const etaMin = Math.round(chunks * 45 / 2 / 60)
  const etaText = etaMin >= 60 ? `${(etaMin / 60).toFixed(1)} 小时` : `${etaMax(etaMin, 1)} 分钟`
  return { chunks, etaMin, etaText }
}
const etaMax = (v: number, lo: number) => Math.max(v, lo)

async function doImport(doc: KgDocument, force: boolean) {
  const est = importEstimate(doc)
  if (est.etaMin >= 30) {
    const { Modal } = await import('ant-design-vue')
    const ok = await new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: '大文档导入预估',
        content: `该文档约 ${est.chunks} 块,预计耗时约 ${est.etaText}。` +
          '导入为后台任务,失败会自动续跑(块级断点,只跑剩余部分),期间可离开页面。',
        okText: '开始导入', cancelText: '取消',
        onOk: () => resolve(true), onCancel: () => resolve(false),
      })
    })
    if (!ok) return
  }
  await loadSafely(async () => {
    const task = await importKgDocument(props.kbId, doc.id, force)
    taskByDoc.value[doc.id] = task
    message.info(`导入任务已提交(任务 ${task.id.slice(0, 8)}…),进度见下方与「任务中心」`)
  })
  await load()
}

async function doImportAll() {
  importingAll.value = true
  await loadSafely(async () => {
    const r = await importKgAll(props.kbId)
    r.tasks.forEach((t) => {
      const docId = (t.payload as { doc_id?: string } | null)?.doc_id
      if (docId) taskByDoc.value[docId] = t
    })
    message.success(`已提交 ${r.tasks.length} 个导入任务${r.skipped.length ? `,跳过 ${r.skipped.length} 个` : ''}`)
  })
  importingAll.value = false
  await load()
}

// ── 块明细抽屉:状态可视 + 定向重试 ───────────────────────────

const chunkColumns = [
  { title: '序号', key: 'seq', width: 70 },
  { title: '状态', key: 'status', width: 90 },
  { title: '字数', key: 'charCount', width: 80 },
  { title: '引用', key: 'hitCount', width: 90 },
  { title: '内容预览', key: 'preview' },
  { title: '操作', key: 'actions', width: 80 },
]

const chunksDrawer = ref<{
  open: boolean
  doc: KgDocument | null
  loading: boolean
  payload: KgChunksPayload | null
}>({ open: false, doc: null, loading: false, payload: null })

function chunkStatusLabel(s: string): string {
  return s === 'done' ? '完成' : s === 'failed' ? '失败' : '待处理'
}
function chunkStatusColor(s: string): string {
  return s === 'done' ? 'green' : s === 'failed' ? 'red' : 'orange'
}

async function openChunks(doc: KgDocument) {
  chunksDrawer.value.open = true
  chunksDrawer.value.doc = doc
  chunksDrawer.value.loading = true
  await loadSafely(async () => {
    chunksDrawer.value.payload = await fetchKgChunks(props.kbId, doc.id)
  })
  chunksDrawer.value.loading = false
}

/** 定向重试提交后的公共收尾:关抽屉、挂行内任务、刷新列表。 */
async function afterRetrySubmitted(task: TaskItem, docId: string) {
  taskByDoc.value[docId] = task
  message.info(`重试任务已提交(任务 ${task.id.slice(0, 8)}…),只补指定块`)
  chunksDrawer.value.open = false
  await load()
  emit('changed')
}

async function retryOneChunk(chunk: KgChunkItem) {
  const doc = chunksDrawer.value.doc
  if (!doc) return
  await loadSafely(async () => {
    const task = await retryKgChunks(props.kbId, doc.id, [chunk.id])
    await afterRetrySubmitted(task, doc.id)
  })
}

async function retryFailedChunks() {
  const doc = chunksDrawer.value.doc
  const payload = chunksDrawer.value.payload
  if (!doc || !payload) return
  const failedIds = payload.items.filter((c) => c.status !== 'done').map((c) => c.id)
  if (!failedIds.length) return
  await loadSafely(async () => {
    const task = await retryKgChunks(props.kbId, doc.id, failedIds)
    await afterRetrySubmitted(task, doc.id)
  })
}

// 单块产出(块级溯源:该块实际抽出的实体与关系)
const extractionDrawer = ref<{
  open: boolean
  loading: boolean
  seq: number
  data: KgChunkExtraction | null
}>({ open: false, loading: false, seq: -1, data: null })

// ── D27 块编辑:改文本 → 仅重嵌该块向量 ──
const chunkEdit = ref<{
  open: boolean; saving: boolean; seq: number
  chunkId: string; text: string; warning: string
}>({ open: false, saving: false, seq: -1, chunkId: '', text: '', warning: '' })

async function openChunkEdit(chunk: KgChunkItem) {
  const doc = chunksDrawer.value.doc
  if (!doc) return
  chunkEdit.value = {
    open: true, saving: false, seq: chunk.seq, chunkId: chunk.id,
    text: '', warning: '',
  }
  // 列表接口只给 80 字预览,编辑器须拉全文
  await loadSafely(async () => {
    const full = await fetchKgChunkText(props.kbId, doc.id, chunk.id)
    chunkEdit.value.text = full.text
  })
}

async function saveChunkEdit() {
  const doc = chunksDrawer.value.doc
  const ce = chunkEdit.value
  if (!doc || !ce.text.trim()) {
    message.warning('块文本不能为空')
    return
  }
  ce.saving = true
  ce.warning = ''
  try {
    const r = await updateKgChunk(props.kbId, doc.id, ce.chunkId, ce.text)
    if (r.warning) {
      ce.warning = r.warning
      message.warning(r.warning)
    } else {
      message.success(r.vectorUpdated
        ? `块 #${ce.seq} 已保存,向量已重建(图谱需重导同步)`
        : `块 #${ce.seq} 已保存`)
      ce.open = false
      await openChunks(doc)
      emit('changed')
    }
  } catch (e) {
    message.error((e as any)?.response?.data?.detail || '保存失败')
  } finally { ce.saving = false }
}

async function showExtraction(chunk: KgChunkItem) {
  const doc = chunksDrawer.value.doc
  if (!doc) return
  extractionDrawer.value = { open: true, loading: true, seq: chunk.seq, data: null }
  await loadSafely(async () => {
    extractionDrawer.value.data = await fetchKgChunkExtraction(props.kbId, doc.id, chunk.id)
  })
  extractionDrawer.value.loading = false
}

async function doDelete(doc: KgDocument) {  await loadSafely(async () => {
    await deleteKgDocument(props.kbId, doc.id)
    message.success(`文档「${doc.filename}」已删除`)
  })
  await load()
  emit('changed')
}

async function doDownload(doc: KgDocument) {
  await loadSafely(async () => {
    await downloadKgDocument(props.kbId, doc)
  })
}

// ── D30 批量删除 + 使用统计 ─────────────────────────────────
const selectedIds = ref<string[]>([])
const batchDeleting = ref(false)
const usageById = ref<Record<string, number>>({})

async function doBatchDelete() {
  if (!selectedIds.value.length) return
  batchDeleting.value = true
  await loadSafely(async () => {
    const r = await batchDeleteKgDocuments(props.kbId, selectedIds.value)
    if (r.failed) {
      message.warning(`批量删除:${r.deleted} 成功 / ${r.failed} 失败(常见原因:文档导入中)`)
    } else {
      message.success(`已删除 ${r.deleted} 个文档`)
    }
    selectedIds.value = []
  })
  batchDeleting.value = false
  await load()
  emit('changed')
}

async function loadUsage() {
  await loadSafely(async () => {
    const d = await fetchKgUsage(props.kbId)
    const map: Record<string, number> = {}
    for (const it of d.items) map[it.docId] = it.hitTotal
    usageById.value = map
  })
}

// 任务进度轮询(2s;只轮询未终态的;有任务转终态时刷新文档统计)
let timer: number | undefined
async function pollTasks() {
  const entries = Object.entries(taskByDoc.value).filter(([, t]) => !isFinal(t))
  if (!entries.length || document.hidden) return
  let anyFinished = false
  for (const [docId, t] of entries) {
    try {
      const fresh = await fetchTask(t.id)
      taskByDoc.value[docId] = fresh
      if (isFinal(fresh)) anyFinished = true
    } catch { /* 下一轮再试 */ }
  }
  if (anyFinished) {
    load()
    emit('changed')
  }
}

watch(() => props.kbId, () => {
  taskByDoc.value = {}
  uploadResults.value = []
  load()
})
watch(() => props.refreshTick, () => load())
onMounted(() => {
  load()
  timer = window.setInterval(pollTasks, 2000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.dm-upload-icon { font-size: 36px; color: #2f54eb; margin: 14px 0 6px; }
.dm-upload-text { font-size: 14px; color: #1f2937; margin: 0 0 6px; }
.dm-upload-hint { font-size: 12px; color: #9ca3af; margin-bottom: 14px; }
.dm-chunk-link { color: #6b7280; font-size: 12px; margin-left: 10px; cursor: pointer; }
.dm-chunk-link:hover { color: #2f54eb; }
.dm-chunk-pop { max-width: 420px; }
.dm-chunk-pop-title { font-weight: 600; color: #1f2937; margin-bottom: 4px; font-size: 12px; }
.dm-chunk-list { margin: 0; padding-left: 16px; color: #6b7280; font-size: 12px; line-height: 1.9; }
.dm-upload-line { font-size: 12px; color: #4b5563; display: flex; align-items: center; gap: 6px; }
.dm-ok { color: #16a34a; }
.dm-bad { color: #dc2626; }
.dm-reason { color: #dc2626; }
.dm-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 16px 0; }
.dm-tip { color: #9ca3af; font-size: 12px; }
.dm-file-icon { margin-right: 8px; color: #64748b; }
.dm-file-icon.pdf { color: #dc2626; }
.dm-file-icon.docx { color: #2563eb; }
.dm-file-icon.md, .dm-file-icon.markdown, .dm-file-icon.txt { color: #6b7280; }
.dm-fname { color: #374151; font-size: 13px; }
.dm-mono { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; color: #6b7280; }
.dm-counts { font-size: 12px; color: #6b7280; }
.dm-done { color: #16a34a; font-size: 12px; font-weight: 600; }
.dm-danger { color: #dc2626; }
.dm-chunks__summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }
.dm-chunks__spacer { flex: 1; }
.dm-chunks__preview {
  display: inline-block; max-width: 340px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
  color: #6b7280; font-size: 12px;
}
.dm-chunks__dim { color: #d1d5db; }
.dm-chunks__hint { color: #9ca3af; font-size: 12px; margin-top: 10px; }
.dm-chunk-edit-warn { margin-top: 8px; color: #d97706; font-size: 12px; }
.dm-meta-tag { font-size: 12px; cursor: pointer; }
.dm-meta-add { font-size: 12px; color: #94a3b8; }
.dm-meta-add:hover { color: #2f54eb; }
.dm-meta-hint { font-size: 12px; color: #94a3b8; margin-bottom: 10px; }
.dm-meta-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
.dm-ext__section { margin-bottom: 18px; }
.dm-ext__title { font-weight: 600; font-size: 13px; color: #374151; margin-bottom: 8px; }
.dm-ext__row { margin-bottom: 6px; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.dm-ext__name { font-weight: 600; color: #111827; }
.dm-ext__desc { color: #9ca3af; font-size: 12px; flex-basis: 100%; }
.dm-ext__rel { background: #f3f4f6; border-radius: 4px; padding: 2px 6px; font-size: 12px; }
.dm-ext__evidence { color: #9ca3af; font-size: 12px; flex-basis: 100%; }
</style>
