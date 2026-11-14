<template>
  <div class="anno">
    <div class="anno-head">
      <div class="anno-title">标准问答</div>
      <a-popover placement="bottomLeft" trigger="click">
        <template #content>
          <div class="anno-tip">
            <p>用户提问与已启用的标注问题相似度达到 0.90 时，直接返回标准答案。</p>
            <p>配置了触发问法的条目：用户原样问出（忽略大小写、空格与尾部标点）时立即返回答案，不经过模型与检索。</p>
            <p>请定期检查答案是否仍适用于当前业务。</p>
          </div>
        </template>
        <a class="anno-info"><QuestionCircleOutlined /></a>
      </a-popover>
      <div style="flex: 1"></div>
      <a-button type="primary" size="small" @click="openCreate"><PlusOutlined /> 新增问答</a-button>
    </div>

    <a-table
      :columns="cols" :data-source="items" :loading="loading" row-key="id"
      size="small" :scroll="{ x: 640 }" :pagination="{ pageSize: 20, showTotal: (t: number) => `共 ${t} 条` }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'question'">
          <span class="anno-q" :title="record.question">{{ record.question }}</span>
        </template>
        <template v-else-if="column.key === 'answer'">
          <span class="anno-a" :title="record.answer">{{ record.answer }}</span>
        </template>
        <template v-else-if="column.key === 'triggers'">
          <a-tooltip v-if="record.triggers?.length"
                     :title="record.triggers.join('；')">
            <a-tag color="blue">固定问答 {{ record.triggers.length }} 问</a-tag>
          </a-tooltip>
          <span v-else class="anno-mute">仅语义</span>
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-switch :checked="record.enabled" size="small"
                    :loading="toggling === record.id"
                    @click="(v: boolean | undefined) => toggle(record, !!v)" />
        </template>
        <template v-else-if="column.key === 'actions'">
          <a @click="openEdit(record)"><EditOutlined /> 编辑</a>
          <a-popconfirm title="删除该问答?" ok-text="删除" ok-type="danger"
                        @confirm="remove(record)">
            <a style="margin-left: 10px" class="anno-del"><DeleteOutlined /> 删除</a>
          </a-popconfirm>
        </template>
      </template>
    </a-table>

    <a-modal v-model:open="editor.open" :title="editor.id ? '编辑问答' : '新增问答'"
             :confirm-loading="editor.saving" ok-text="保存" cancel-text="取消" @ok="save">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item required>
          <template #label>
            <span>问法(每行一个)</span>
            <a-popover placement="right" trigger="click">
              <template #content>
                <div class="anno-tip">
                  <p>「问法」每行一个：<b>第一行是标准问题</b>（列表展示、语义匹配基准）；其余为触发问法——用户原样问出（忽略大小写、空格与尾部标点）即直接返回答案，不经过模型与检索。</p>
                  <p>未命中的其他问法仍可按语义相似（≥0.90）命中本条；同一知识库内问法不可与其他条目重复。</p>
                </div>
              </template>
              <QuestionCircleOutlined class="anno-modal-help" />
            </a-popover>
          </template>
          <a-textarea v-model:value="editor.qaText" :rows="4" :maxlength="2000"
                      placeholder="如:&#10;未成年工的年龄是怎么界定的?&#10;未成年工年龄怎么算&#10;未成年工几岁" />
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
// 标准问答(标注回复 D32)管理:问题→标准答案 CRUD;命中(≥0.90)短路检索。
// 触发问法(固定问答):原样问出即确定命中,零模型调用,先于语义匹配。
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
  { title: '触发问法', key: 'triggers', width: 110 },
  { title: '启用', key: 'enabled', width: 70 },
  { title: '操作', key: 'actions', width: 150 },
]

const editor = ref<{ open: boolean; id: string; qaText: string; answer: string; saving: boolean }>(
  { open: false, id: '', qaText: '', answer: '', saving: false })

async function load() {
  loading.value = true
  await loadSafely(async () => {
    items.value = await fetchKgAnnotations(props.kbId)
  })
  loading.value = false
}

watch(() => props.kbId, load, { immediate: true })

function openCreate() {
  editor.value = { open: true, id: '', qaText: '', answer: '', saving: false }
}
function openEdit(r: KgAnnotation) {
  // 合并问法框:第一行=标准问题,其余=触发问法
  editor.value = { open: true, id: r.id,
                   qaText: [r.question, ...(r.triggers || [])].join('\n'),
                   answer: r.answer, saving: false }
}

// 触发问法按行拆分:去空行/首尾空白,去重(保序)
function splitTriggers(text: string): string[] {
  const seen = new Set<string>()
  return text.split('\n').map(t => t.trim()).filter(t => {
    if (!t || seen.has(t)) return false
    seen.add(t)
    return true
  })
}

async function save() {
  const e = editor.value
  // 合并问法框拆分:第一行=标准问题,其余=触发问法(去空/去重,与 question 重复自动剔除)
  const lines = e.qaText.split('\n').map(t => t.trim()).filter(Boolean)
  const question = lines[0] || ''
  const answer = e.answer
  if (!question || !answer.trim()) {
    message.warning('问法(第一行)和答案都不能为空')
    return
  }
  const triggers = splitTriggers(lines.slice(1).join('\n'))
  e.saving = true
  try {
    await saveKgAnnotation(props.kbId, e.id || null, question, answer.trim(), triggers)
    message.success(e.id ? '问答已更新' : '问答已创建(相似提问将直接命中)')
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
    message.success('问答已删除')
    await load()
  } catch (err) {
    message.error((err as any)?.response?.data?.detail || '删除失败')
  }
}
</script>

<style scoped>
.anno-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }
.anno-title { font-weight: 600; font-size: 16px; color: #1f2937; }
.anno-info { color: #94a3b8; cursor: pointer; }
.anno-info:hover { color: var(--brand-ink); }
.anno-tip { max-width: 330px; font-size: 12px; line-height: 1.8; color: #4b5563; }
.anno-tip p { margin: 0; }
.anno-q { color: #1f2937; font-weight: 500; }
.anno-a { color: #4b5563; }
.anno-mute { color: #94a3b8; font-size: 12px; }
.anno-modal-help { color: #94a3b8; cursor: pointer; margin-left: 4px; }
.anno-modal-help:hover { color: var(--brand-ink); }
.anno-del { color: #ef4444; }
</style>
