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
      <p class="dm-upload-hint">支持 md / txt / pdf / docx,单文件 ≤ {{ maxMb }}MB;同库同内容自动去重</p>
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

    <!-- 工具条 -->
    <div class="dm-toolbar">
      <a-button type="primary" :disabled="!importableCount" :loading="importingAll" @click="doImportAll">
        <CloudUploadOutlined /> 导入全部未处理文档
      </a-button>
      <span class="dm-tip">导入 = 后台任务:LLM 抽取实体关系 → Neo4j 图谱 + Milvus 向量;任务中心可查日志</span>
    </div>

    <!-- 文档表 -->
    <a-table
      :columns="columns" :data-source="docs" :loading="loading" row-key="id"
      size="middle" :pagination="{ pageSize: 20, showTotal: (t: number) => `共 ${t} 条` }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'filename'">
          <FileTextOutlined class="dm-file-icon" :class="extOf(record.filename)" />
          <span class="dm-fname">{{ record.filename }}</span>
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
          <a
            v-if="record.importStatus !== 'importing'"
            @click="doImport(record, false)"
          ><CloudUploadOutlined /> {{ record.importStatus === 'succeeded' ? '重导' : '导入' }}</a>
          <a-tooltip title="忽略已有 checkpoint 与内容判断,全部重抽(本体变更后用)">
            <a style="margin-left: 12px" @click="doImport(record, true)"><RedoOutlined /> 强制</a>
          </a-tooltip>
          <a-popconfirm title="删除文档将清除其图谱贡献与向量,确认?" @confirm="doDelete(record)">
            <a class="dm-danger" style="margin-left: 12px"><DeleteOutlined /> 删除</a>
          </a-popconfirm>
        </template>
      </template>
    </a-table>
  </div>
</template>

<script setup lang="ts">
// 文档管理:拖拽上传(查重/预检) + 导入任务发起 + 行内任务进度轮询。
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { UploadChangeParam } from 'ant-design-vue'
import {
  CheckCircleOutlined, CloudUploadOutlined, CloseCircleOutlined, DeleteOutlined,
  FileTextOutlined, InboxOutlined, RedoOutlined,
} from '@ant-design/icons-vue'
import {
  KgDocument, TaskItem, TaskStatus, deleteKgDocument, fetchKgDocuments,
  fetchTask, importKgAll, importKgDocument, uploadKgDocuments,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string; refreshTick?: number }>()
const emit = defineEmits<{ (e: 'changed'): void }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const docs = ref<KgDocument[]>([])
const loading = ref(false)
const uploading = ref(false)
const importingAll = ref(false)
const uploadResults = ref<{ filename: string; ok: boolean; reason?: string }[]>([])
const taskByDoc = ref<Record<string, TaskItem>>({})
const maxMb = 20
let loadSeq = 0   // 请求序号守卫(切库错序防护)

const columns = [
  { title: '文件', key: 'filename', ellipsis: true },
  { title: '大小', key: 'size', width: 90 },
  { title: '导入状态', key: 'importStatus', width: 110 },
  { title: '统计', key: 'counts', width: 190 },
  { title: '任务进度', key: 'progress', width: 150 },
  { title: '操作', key: 'actions', width: 240 },
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
      const okCount = uploadResults.value.filter((r) => r.ok).length
      if (okCount) message.success(`${okCount} 个文件上传成功`)
    })
  } finally {
    uploading.value = false
  }
  await load()
  emit('changed')
}

async function doImport(doc: KgDocument, force: boolean) {
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

async function doDelete(doc: KgDocument) {
  await loadSafely(async () => {
    await deleteKgDocument(props.kbId, doc.id)
    message.success(`文档「${doc.filename}」已删除`)
  })
  await load()
  emit('changed')
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
.dm-upload-line { font-size: 12.5px; color: #4b5563; display: flex; align-items: center; gap: 6px; }
.dm-ok { color: #16a34a; }
.dm-bad { color: #dc2626; }
.dm-reason { color: #dc2626; }
.dm-toolbar { display: flex; align-items: center; gap: 12px; margin: 14px 0 12px; }
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
</style>
