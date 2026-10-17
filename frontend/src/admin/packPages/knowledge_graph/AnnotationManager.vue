<template>
  <div class="anno">
    <div class="anno-head">
      <div class="anno-title">标注回复</div>
      <a-popover placement="bottomLeft" trigger="click">
        <template #content>
          <div class="anno-tip">
            <p>知识库"老答错同一个问题"的治本件:录入「问题 → 标准答案」,</p>
            <p>用户提问与标注问题<b>强相似</b>(≥0.90)时直接返回标准答案,不走检索。</p>
            <p>点踩的记录可整理成标录入库;错过的知识补课一次,终身正确。</p>
          </div>
        </template>
        <a class="anno-info"><QuestionCircleOutlined /></a>
      </a-popover>
      <div style="flex: 1"></div>
      <a-button type="primary" size="small" @click="openCreate"><PlusOutlined /> 新增标注</a-button>
    </div>

    <a-table
      :columns="cols" :data-source="items" :loading="loading" row-key="id"
      size="small" :pagination="{ pageSize: 20, showTotal: (t: number) => `共 ${t} 条` }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'question'">
          <span class="anno-q" :title="record.question">{{ record.question }}</span>
        </template>
        <template v-else-if="column.key === 'answer'">
          <span class="anno-a" :title="record.answer">{{ record.answer }}</span>
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-switch :checked="record.enabled" size="small"
                    :loading="toggling === record.id"
                    @click="(v: boolean | undefined) => toggle(record, !!v)" />
        </template>
        <template v-else-if="column.key === 'actions'">
          <a @click="openEdit(record)"><EditOutlined /> 编辑</a>
          <a-popconfirm title="删除该标注?" ok-text="删除" ok-type="danger"
                        @confirm="remove(record)">
            <a style="margin-left: 10px" class="anno-del"><DeleteOutlined /> 删除</a>
          </a-popconfirm>
        </template>
      </template>
    </a-table>

    <a-modal v-model:open="editor.open" :title="editor.id ? '编辑标注' : '新增标注'"
             :confirm-loading="editor.saving" ok-text="保存" cancel-text="取消" @ok="save">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="问题(用户会怎么问)" required>
          <a-textarea v-model:value="editor.question" :rows="2" :maxlength="500"
                      placeholder="如:未成年工的年龄是怎么界定的?" show-count />
        </a-form-item>
        <a-form-item label="标准答案" required>
          <a-textarea v-model:value="editor.answer" :rows="4" :maxlength="4000"
                      placeholder="命中后直接返回此答案(不走检索)" show-count />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// D32 标注回复管理:问题→标准答案 CRUD;命中(≥0.90)短路检索。
import { inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  DeleteOutlined, EditOutlined, PlusOutlined, QuestionCircleOutlined,
} from '@ant-design/icons-vue'
import { KgAnnotation, fetchKgAnnotations, saveKgAnnotation,
         setKgAnnotationEnabled, deleteKgAnnotation } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const items = ref<KgAnnotation[]>([])
const loading = ref(false)
const toggling = ref('')

const cols = [
  { title: '问题', key: 'question', ellipsis: true },
  { title: '标准答案', key: 'answer', ellipsis: true },
  { title: '启用', key: 'enabled', width: 70 },
  { title: '操作', key: 'actions', width: 150 },
]

const editor = ref<{ open: boolean; id: string; question: string; answer: string; saving: boolean }>(
  { open: false, id: '', question: '', answer: '', saving: false })

async function load() {
  loading.value = true
  await loadSafely(async () => {
    items.value = await fetchKgAnnotations(props.kbId)
  })
  loading.value = false
}

watch(() => props.kbId, load, { immediate: true })

function openCreate() {
  editor.value = { open: true, id: '', question: '', answer: '', saving: false }
}
function openEdit(r: KgAnnotation) {
  editor.value = { open: true, id: r.id, question: r.question, answer: r.answer, saving: false }
}

async function save() {
  const e = editor.value
  if (!e.question.trim() || !e.answer.trim()) {
    message.warning('问题和答案都不能为空')
    return
  }
  e.saving = true
  try {
    await saveKgAnnotation(props.kbId, e.id || null, e.question.trim(), e.answer.trim())
    message.success(e.id ? '标注已更新' : '标注已创建(相似提问将直接命中)')
    e.open = false
    await load()
  } catch (err) {
    message.error((err as any)?.response?.data?.detail || '保存失败')
  } finally { e.saving = false }
}

async function toggle(r: KgAnnotation, v: boolean) {
  toggling.value = r.id
  try {
    await setKgAnnotationEnabled(props.kbId, r.id, v)
    r.enabled = v
  } catch (err) {
    message.error((err as any)?.response?.data?.detail || '操作失败')
  } finally { toggling.value = '' }
}

async function remove(r: KgAnnotation) {
  try {
    await deleteKgAnnotation(props.kbId, r.id)
    message.success('标注已删除')
    await load()
  } catch (err) {
    message.error((err as any)?.response?.data?.detail || '删除失败')
  }
}
</script>

<style scoped>
.anno-head { display: flex; align-items: center; gap: 6px; margin-bottom: 12px; }
.anno-title { font-weight: 600; font-size: 13.5px; color: #1f2937; }
.anno-info { color: #94a3b8; cursor: pointer; }
.anno-info:hover { color: #2f54eb; }
.anno-tip { max-width: 330px; font-size: 12.5px; line-height: 1.8; color: #4b5563; }
.anno-tip p { margin: 0; }
.anno-q { color: #1f2937; font-weight: 500; }
.anno-a { color: #4b5563; }
.anno-del { color: #ef4444; }
</style>
