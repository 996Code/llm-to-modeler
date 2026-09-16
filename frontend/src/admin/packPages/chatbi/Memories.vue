<template>
  <div class="mem-page">
    <!-- 工具栏:按钮 + 分类筛选 + 整理/新建合并到一行 -->
    <div class="mem-toolbar">
      <a-select v-model:value="dsFilter" size="small" style="min-width: 160px"
                placeholder="全部数据源" allow-clear @change="loadMemories">
        <a-select-option v-for="d in datasources" :key="d.id" :value="d.id">{{ d.name }}</a-select-option>
      </a-select>
      <a-button size="small" @click="consolidate" :loading="consolidating">
        <ForkOutlined /> 整理记忆
      </a-button>
      <a-button type="primary" size="small" @click="openCreate">
        <PlusOutlined /> 新建记忆
      </a-button>
      <a-radio-group v-model:value="typeFilter" size="small" @change="loadMemories">
        <a-radio-button value="">全部</a-radio-button>
        <a-radio-button value="project">项目</a-radio-button>
        <a-radio-button value="preference">偏好</a-radio-button>
        <a-radio-button value="business">业务</a-radio-button>
        <a-radio-button value="linkage">表关联</a-radio-button>
      </a-radio-group>
      <a-checkbox v-model:checked="showConsolidated" @change="loadMemories">
        显示已整理
      </a-checkbox>
      <a-popconfirm v-if="orphanCount" :title="`把 ${orphanCount} 条无归属记忆全部归属到当前选中的数据源?`"
                    ok-text="归属" ok-type="danger" @confirm="backfillScope">
        <a-button size="small" danger>
          <LinkOutlined /> 归属到当前库 ({{ orphanCount }})
        </a-button>
      </a-popconfirm>
      <span class="mem-count">共 <b>{{ memories.length }}</b> 条</span>
    </div>
    <div class="mem-hint">
      <InfoCircleOutlined /> 生效中的记忆会在每次查询时按关键词召回并注入 SQL 生成;已整理的原始记忆已并入新记忆, 不再参与召回
    </div>

      <a-table :data-source="filtered" :loading="loading" row-key="id" size="small"
               :pagination="filtered.length > 20 ? { pageSize: 20 } : false">
        <a-table-column title="名称" data-index="name" width="180">
          <template #default="{ record }">
            <b>{{ record.name }}</b>
          </template>
        </a-table-column>
        <a-table-column title="类型" width="90">
          <template #default="{ record }">
            <a-tag :color="typeColor(record.type)">{{ typeLabel(record.type) }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="归属" width="130">
          <template #default="{ record }">
            <span v-if="record.data_source_id" class="mem-scope">
              {{ dsName(record.data_source_id) }}{{ record.user_id ? ' · 用户' + record.user_id : ' · 全局' }}
            </span>
            <a-tag v-else color="red" title="无归属的记忆不会被问数召回">无归属</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="描述" data-index="description" :ellipsis="true" />
        <a-table-column title="内容" width="320">
          <template #default="{ record }">
            <a-tooltip :title="record.content" placement="topLeft">
              <span class="mem-content">{{ record.content }}</span>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="状态" width="90">
          <template #default="{ record }">
            <a-tag v-if="record.consolidated" color="default">已整理</a-tag>
            <a-tag v-else-if="!record.data_source_id" color="red"
                   title="无归属的记忆不参与问数召回, 请用「归属到当前库」修复">未生效</a-tag>
            <a-tag v-else color="green">生效中</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="操作" width="150">
          <template #default="{ record }">
            <a-tooltip v-if="record.type === 'linkage'" title="linkage 是查询沉淀的结构化数据, 不提供文本编辑">
              <a-button size="small" type="link" disabled>编辑</a-button>
            </a-tooltip>
            <a-button v-else size="small" type="link" @click="openEdit(record)">编辑</a-button>
            <a-popconfirm title="删除该记忆?" ok-text="删除" ok-type="danger"
                          @confirm="removeMem(record)">
              <a-button size="small" type="link" danger>删除</a-button>
            </a-popconfirm>
          </template>
        </a-table-column>
      </a-table>
      <a-empty v-if="!loading && !filtered.length" description="暂无记忆——对话中会自动沉淀,也可手动新建业务约定" />

    <!-- 新建/编辑记忆(双栏) -->
    <a-modal v-model:open="showForm" :title="form.mem_id ? '编辑记忆' : '新建记忆'"
             ok-text="保存" cancel-text="取消" :confirm-loading="saving" @ok="save">
      <a-form layout="vertical" class="mem-form">
        <div class="form-row">
          <a-form-item label="名称" required class="half">
            <a-input v-model:value="form.name" placeholder="如:GMV 口径约定" :maxlength="60" />
          </a-form-item>
          <a-form-item label="类型" class="half">
            <a-select v-model:value="form.memory_type">
              <a-select-option value="project">项目</a-select-option>
              <a-select-option value="preference">偏好</a-select-option>
              <a-select-option value="business">业务</a-select-option>
            </a-select>
          </a-form-item>
        </div>
        <a-form-item label="数据源" required>
          <a-select v-model:value="form.data_source_id" placeholder="选择数据源(全局规则, 对该库所有用户生效)">
            <a-select-option v-for="d in datasources" :key="d.id" :value="d.id">{{ d.name }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="form.description" placeholder="一句话说明" :maxlength="120" />
        </a-form-item>
        <a-form-item label="内容(Markdown)" required>
          <a-textarea v-model:value="form.content" :rows="5"
                      placeholder="如:GMV = 已支付且未退款的订单总金额(含税)" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 记忆管理子页(对标原版 MemoryView 的核心子集):列表/筛选/新建/编辑/删除/整理。
// 整理走异步任务(任务中心观测进度), 完成后自动刷新列表。
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  BulbOutlined, ForkOutlined, InfoCircleOutlined, LinkOutlined, PlusOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'
import { tasksApi } from '../../api'

const memories = ref<any[]>([])
const loading = ref(false)
const typeFilter = ref('')
const dsFilter = ref<string | undefined>(undefined)
const datasources = ref<{ id: string; name: string }[]>([])

function dsName(id: string): string {
  return datasources.value.find((d) => d.id === id)?.name || id.slice(0, 8) + '…'
}
const showConsolidated = ref(true)
const consolidating = ref(false)

const showForm = ref(false)
const saving = ref(false)
const form = reactive({ mem_id: '', name: '', description: '', content: '', memory_type: 'project', data_source_id: '' })

const filtered = computed(() =>
  typeFilter.value ? memories.value.filter((m) => m.type === typeFilter.value) : memories.value)

// 无归属记忆数(批量归属工具的显隐与文案)
const orphanCount = computed(() =>
  memories.value.filter((m) => !m.data_source_id).length)

async function backfillScope() {
  if (!dsFilter.value) {
    message.warning('请先在左侧选择目标数据源, 再执行归属')
    return
  }
  try {
    const { data } = await chatbiApi.post('/memories/backfill-scope',
      { data_source_id: dsFilter.value })
    message.success(`已归属 ${data.backfilled} 条记忆到当前库——立即参与问数召回`)
    await loadMemories()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '归属失败')
  }
}

function typeLabel(t: string): string {
  return { project: '项目', preference: '偏好', business: '业务', linkage: '表关联' }[t] || t || '-'
}
function typeColor(t: string): string {
  return { project: 'blue', preference: 'purple', business: 'gold', linkage: 'cyan' }[t] || 'default'
}

async function loadMemories() {
  loading.value = true
  try {
    const { data } = await chatbiApi.get('/memories', {
      params: { limit: 200, include_consolidated: showConsolidated.value,
                data_source_id: dsFilter.value || undefined },
    })
    memories.value = data.items || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  } finally { loading.value = false }
}

function openCreate() {
  Object.assign(form, { mem_id: '', name: '', description: '', content: '', memory_type: 'project',
                        data_source_id: dsFilter.value || '' })
  showForm.value = true
}

function openEdit(record: any) {
  Object.assign(form, {
    mem_id: record.id, name: record.name, description: record.description || '',
    content: record.content || '', memory_type: record.type || 'project',
    data_source_id: record.data_source_id || '',
  })
  showForm.value = true
}

async function save() {
  if (!form.name.trim() || !form.content.trim()) {
    message.warning('请填写名称与内容')
    return
  }
  // 新建必须选数据源(scope 契约: 手工记忆 = 数据源全局规则; 无归属召不回)
  if (!form.mem_id && !form.data_source_id) {
    message.warning('请选择数据源——手工记忆是该库的全局规则, 对该库所有用户生效')
    return
  }
  saving.value = true
  try {
    await chatbiApi.put('/memories', {
      name: form.name, description: form.description, content: form.content,
      memory_type: form.memory_type, mem_id: form.mem_id || null,
      data_source_id: form.data_source_id || null,
    })
    message.success(form.mem_id ? '记忆已更新' : '记忆已创建')
    showForm.value = false
    await loadMemories()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '保存失败')
  } finally { saving.value = false }
}

async function removeMem(record: any) {
  try {
    await chatbiApi.delete(`/memories/${record.id}`)
    message.success('已删除')
    await loadMemories()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '删除失败')
  }
}

async function consolidate() {
  consolidating.value = true
  try {
    const { data } = await chatbiApi.post('/memories/consolidate',
      dsFilter.value ? { data_source_id: dsFilter.value } : {})
    message.success(`整理任务已提交(任务 ${String(data.task_id).slice(0, 8)}…)——进度与日志可在任务中心查看`)
    // 轮询任务完成(整理通常 10-30s;完成后刷新列表看到新记忆)
    pollTaskDone(data.task_id)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '提交失败')
  } finally { consolidating.value = false }
}

// 轮询任务终态:完成/失败后刷新列表并提示整理结果(不再让用户去任务中心找结果)
let pollTimer: ReturnType<typeof setTimeout> | null = null
function pollTaskDone(taskId: string, attempt = 0) {
  if (pollTimer) clearTimeout(pollTimer)
  if (attempt > 30) return  // 最多 ~90s
  pollTimer = setTimeout(async () => {
    try {
      const { data } = await tasksApi.get(`/${taskId}`)
      if (data.status === 'succeeded') {
        await loadMemories()
        const r = data.result || {}
        if (r.consolidated) {
          message.success(`整理完成: 合并为 ${r.consolidated} 条精炼记忆(原 ${r.total} 条已标记隐藏)`)
        } else {
          message.info(`整理完成: ${r.detail || '无需整理'}`)
        }
        return
      }
      if (data.status === 'failed') {
        message.error(`整理失败: ${data.error || '未知错误'}`)
        return
      }
    } catch { /* 轮询失败继续 */ }
    pollTaskDone(taskId, attempt + 1)
  }, 3000)
}

onMounted(() => {
  chatbiApi.get('/datasources').then(({ data }) => {
    datasources.value = (data.items || []).map((d: any) => ({ id: d.id, name: d.name }))
  }).catch(() => { /* 静默 */ })
  loadMemories()
})
</script>

<style scoped>
.mem-page { display: flex; flex-direction: column; }
.mem-toolbar {
  display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
  font-size: 13px; flex-wrap: wrap;
}
.mem-count { color: #86909c; margin-left: auto; }
.mem-count b { color: #1d2129; }
.mem-hint { font-size: 12px; color: #999; margin-bottom: 12px; }
.mem-scope { font-size: 12px; color: #4e5969; }
.mem-content {
  display: inline-block; max-width: 300px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 12px; color: #4e5969; vertical-align: middle;
}
.mem-form .form-row { display: flex; gap: 12px; }
.mem-form .half { flex: 1; }
</style>
