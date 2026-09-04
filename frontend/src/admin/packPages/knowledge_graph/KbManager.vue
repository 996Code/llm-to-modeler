<template>
  <div>
    <div class="km-toolbar">
      <a-button type="primary" @click="openCreate"><PlusOutlined /> 新建知识库</a-button>
      <span class="km-tip">每个库独立本体 / 图谱 / 向量 collection(物理隔离,删库即清)</span>
    </div>

    <div class="km-grid">
      <div v-for="kb in kbs" :key="kb.id" class="km-card" :class="{ active: kb.id === props.selected }">
        <div class="km-head">
          <div class="km-icon"><DatabaseOutlined /></div>
          <div class="km-title-block">
            <div class="km-name">{{ kb.name }}</div>
            <div class="km-tags">
              <a-tag v-if="kb.id === props.selected" color="processing">当前</a-tag>
              <a-tag v-if="kb.vectorEnabled" color="cyan">向量 dim={{ kb.vectorDim }}</a-tag>
              <a-tag v-else>纯图谱</a-tag>
              <a-tag v-if="kb.schemaTemplate" color="geekblue">{{ kb.schemaTemplate }}</a-tag>
            </div>
          </div>
        </div>
        <div class="km-desc">{{ kb.description || '(无描述)' }}</div>
        <div class="km-stats">
          <div class="km-stat"><DatabaseOutlined /> 文档 <b>{{ kb.docCount ?? 0 }}</b></div>
          <div class="km-stat"><NodeIndexOutlined /> 实体 <b>{{ kb.entityTotal ?? 0 }}</b></div>
          <div class="km-stat"><ApartmentOutlined /> 关系 <b>{{ kb.relationTotal ?? 0 }}</b></div>
        </div>
        <div class="km-actions">
          <a @click="$emit('select', kb.id)"><ArrowRightOutlined /> 进入</a>
          <a-popconfirm
            title="删除知识库将同时清除图谱、向量与上传文件,确认?"
            ok-text="删除" ok-type="danger" @confirm="doDelete(kb)"
          >
            <a class="km-danger"><DeleteOutlined /> 删除</a>
          </a-popconfirm>
        </div>
      </div>
    </div>
    <a-empty v-if="!loading && !kbs.length" description="还没有知识库,先建一个" style="padding: 50px 0" />

    <!-- 建库弹窗:选本体模板起步 -->
    <a-modal v-model:open="createOpen" title="新建知识库" :confirm-loading="creating" @ok="doCreate">
      <a-form layout="vertical" style="margin-top: 8px">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" placeholder="如:产品手册、组织人事库" :maxlength="60" />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="form.description" placeholder="可选" :maxlength="200" />
        </a-form-item>
        <a-form-item label="本体模板(建库后可在「本体设置」页完全自定义)">
          <div class="km-templates">
            <div
              v-for="t in templates" :key="t.key" class="km-tpl"
              :class="{ on: form.template === t.key }" @click="form.template = t.key"
            >
              <div class="km-tpl-title">{{ t.title }}</div>
              <div class="km-tpl-desc">{{ t.description }}</div>
              <div class="km-tpl-meta">{{ t.entityCount }} 实体类型 / {{ t.relationCount }} 关系类型</div>
            </div>
          </div>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 知识库管理:卡片列表 + 模板化建库 + 级联删除(图/向量/文件)。
import { inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ApartmentOutlined, ArrowRightOutlined, DatabaseOutlined, DeleteOutlined,
  NodeIndexOutlined, PlusOutlined,
} from '@ant-design/icons-vue'
import {
  KgKnowledgeBase, KgTemplate, createKgKb, deleteKgKb, fetchKgTemplates,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbs: KgKnowledgeBase[]; loading: boolean; selected?: string }>()
const emit = defineEmits<{ (e: 'refresh'): void; (e: 'select', id: string): void }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const templates = ref<KgTemplate[]>([])
const createOpen = ref(false)
const creating = ref(false)
const form = ref({ name: '', description: '', template: 'general' })

onMounted(async () => {
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
    emit('refresh')
    emit('select', kb.id)
  })
  creating.value = false
}

async function doDelete(kb: KgKnowledgeBase) {
  await loadSafely(async () => {
    const r = await deleteKgKb(kb.id)
    if (r.cleanupErrors?.length) {
      message.warning(`已删除,但部分存储清理失败:${r.cleanupErrors.join('; ')}(可重试)`)
    } else {
      message.success(`知识库「${kb.name}」已删除`)
    }
    emit('refresh')
  })
}
</script>

<style scoped>
.km-toolbar { display: flex; align-items: center; gap: 12px; margin: 6px 0 14px; }
.km-tip { color: #9ca3af; font-size: 12px; }
.km-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.km-card {
  border: 1px solid #e6ebf2; border-radius: 12px; padding: 16px 18px; background: #fff;
  transition: box-shadow 0.15s, border-color 0.15s;
}
.km-card:hover { box-shadow: 0 6px 18px rgba(30, 41, 82, 0.08); }
.km-card.active { border-color: #2f54eb; background: #f7f9ff; }
.km-head { display: flex; gap: 12px; align-items: center; }
.km-icon {
  width: 40px; height: 40px; border-radius: 10px; flex-shrink: 0;
  background: linear-gradient(135deg, #eef2ff, #e0e7ff); color: #4f46e5; font-size: 18px;
  display: flex; align-items: center; justify-content: center;
}
.km-title-block { flex: 1; min-width: 0; }
.km-name { font-weight: 700; font-size: 14px; color: #1f2937; }
.km-tags { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.km-desc {
  margin-top: 10px; font-size: 12.5px; color: #6b7280; line-height: 1.5;
  min-height: 20px; word-break: break-all;
}
.km-stats { display: flex; gap: 16px; margin-top: 10px; }
.km-stat { font-size: 12px; color: #6b7280; display: flex; align-items: center; gap: 4px; }
.km-stat b { color: #1f2937; font-size: 14px; }
.km-actions { display: flex; gap: 16px; margin-top: 12px; font-size: 13px; }
.km-actions a { color: #2f54eb; }
.km-actions .km-danger { color: #dc2626; }
.km-templates { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.km-tpl {
  border: 1px solid #e6ebf2; border-radius: 10px; padding: 10px 12px; cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.km-tpl:hover { border-color: #a5b4fc; }
.km-tpl.on { border-color: #2f54eb; background: #f5f7ff; }
.km-tpl-title { font-weight: 600; font-size: 13px; color: #1f2937; }
.km-tpl-desc { font-size: 12px; color: #6b7280; margin: 4px 0 2px; }
.km-tpl-meta { font-size: 11px; color: #94a3b8; }
</style>
