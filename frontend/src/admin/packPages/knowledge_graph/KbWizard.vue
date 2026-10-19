<template>
  <a-modal v-model:open="open" title="新建知识库(向导)" :width="880" :footer="null"
           :mask-closable="false" destroy-on-close>
    <a-steps :current="step" size="small" style="margin: 8px 0 16px">
      <a-step title="基本信息与上传" />
      <a-step title="分段配置" />
      <a-step title="确认导入" />
    </a-steps>

    <!-- 步骤①:基本信息 + 文件上传 -->
    <div v-if="step === 0">
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" placeholder="如:产品手册、组织人事库" :maxlength="60" />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="form.description" placeholder="可选" :maxlength="200" />
        </a-form-item>
        <a-form-item label="所属目录">
          <a-select v-model:value="form.parentId" :options="parentOptions" />
        </a-form-item>
        <a-form-item label="本体模板(建库后可在「本体设置」页完全自定义)">
          <div class="kg-templates">
            <div v-for="t in templates" :key="t.key" class="kg-tpl"
                 :class="{ on: form.template === t.key }" @click="form.template = t.key">
              <div class="kg-tpl-title">{{ t.title }}</div>
              <div class="kg-tpl-desc">{{ t.description }}</div>
              <div class="kg-tpl-meta">{{ t.entityCount }} 实体类型 / {{ t.relationCount }} 关系类型</div>
            </div>
          </div>
        </a-form-item>
        <a-form-item label="上传文档(可跳过,建库后再传)">
          <a-upload-dragger
            :file-list="fileList" :multiple="true" :show-upload-list="true"
            :before-upload="pickFile" accept=".md,.markdown,.txt,.pdf,.docx"
            @remove="removeFile"
          >
            <p class="dm-upload-icon"><InboxOutlined /></p>
            <p class="dm-upload-text">点击或拖拽文件到此处</p>
            <p class="dm-upload-hint">支持 md / txt / pdf / docx,单文件 ≤ 20MB;扫描版 PDF 导入时自动 OCR 识别(pypdfium2 渲染 + RapidOCR)</p>
          </a-upload-dragger>
        </a-form-item>
      </a-form>
    </div>

    <!-- 步骤②:分段配置 + 实时预览 -->
    <div v-else-if="step === 1">
      <ChunkConfigPanel v-model="params" :files="files" />
    </div>

    <!-- 步骤③:确认 -->
    <div v-else class="wiz-summary">
      <div class="wiz-row"><b>名称</b>{{ form.name }}</div>
      <div class="wiz-row"><b>本体模板</b>{{ form.template }}</div>
      <div class="wiz-row">
        <b>文档</b>
        <span v-if="files.length">{{ files.length }} 个文件:{{ files.map((f) => f.name).join('、') }}</span>
        <span v-else>未上传(建库后可在「文档导入」页上传)</span>
      </div>
      <div class="wiz-row">
        <b>分段</b>目标 {{ params.chunk_target_chars }} 字 / 重叠 {{ params.chunk_overlap_chars }} 字 /
        章节块上限 {{ params.chunk_structural_max_chars }} 字 / 向量子切 {{ params.vector_subchunk_chars }} 字
      </div>
      <a-alert type="info" show-icon style="margin-top: 10px"
               message="导入为后台任务:LLM 抽取实体关系 → 图谱 + 向量,任务中心可查进度" />
    </div>

    <!-- 底部导航 -->
    <div class="wiz-footer">
      <a-button v-if="step > 0" @click="step--">上一步</a-button>
      <div style="flex: 1"></div>
      <a-button @click="open = false">取消</a-button>
      <a-button v-if="step < 2" type="primary" :disabled="step === 0 && !form.name.trim()"
                @click="step++">{{ step === 1 ? '下一步' : '下一步' }}</a-button>
      <a-button v-else type="primary" :loading="creating" :disabled="creating"
                @click="doFinish">创建并导入</a-button>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
// D33+D8 建库三步向导:①基本信息+上传 ②分段配置+实时预览 ③确认导入。
// 替换原"新建库"简单弹窗:分段参数从"藏在插件设置里改一次导一次试一次"
// 变为建库时可视化调参(预览与导入同一条切块代码路径)。
import { computed, inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { InboxOutlined } from '@ant-design/icons-vue'
import {
  KgChunkParams, KgTemplate, createKgKb, importKgAll, uploadKgDocuments,
} from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import ChunkConfigPanel from './ChunkConfigPanel.vue'

const props = defineProps<{ templates: KgTemplate[]; parentOptions: { value: string; label: string }[] }>()
const emit = defineEmits<{ (e: 'created', kbId: string): void }>()
const loadSafely = inject<LoadSafely>('loadSafely')!
const open = ref(false)
const step = ref(0)
const creating = ref(false)
const form = ref({ name: '', description: '', template: 'general', parentId: '' })
const files = ref<File[]>([])
const fileList = ref<{ uid: number; name: string; status?: string; originFileObj?: File }[]>([])

// 分段参数默认值(与后端/插件设置默认一致;用户调完随建库落为库级参数)
const params = ref<Required<KgChunkParams>>({
  chunk_target_chars: 1200,
  chunk_overlap_chars: 100,
  chunk_max_chars: 3000,
  chunk_structural_max_chars: 10000,
  vector_subchunk_chars: 500,
})

function reset() {
  step.value = 0
  form.value = { name: '', description: '', template: 'general', parentId: '' }
  files.value = []
  fileList.value = []
  params.value = {
    chunk_target_chars: 1200, chunk_overlap_chars: 100, chunk_max_chars: 3000,
    chunk_structural_max_chars: 10000, vector_subchunk_chars: 500,
  }
}

watch(open, (v) => { if (v) reset() })

// 上传拦截:文件留在本地(向导结束才真正上传),不自动发请求
function pickFile(f: File) {
  files.value = [...files.value, f]
  fileList.value = [...fileList.value, { uid: Date.now() + Math.random(), name: f.name, originFileObj: f }]
  return false
}
function removeFile(f: { name: string }) {
  const idx = files.value.findIndex((x) => x.name === f.name)
  if (idx >= 0) files.value = files.value.filter((_, i) => i !== idx)
  fileList.value = fileList.value.filter((x) => x.name !== f.name)
}

async function doFinish() {
  if (!form.value.name.trim()) return
  creating.value = true
  await loadSafely(async () => {
    const kb = await createKgKb({
      name: form.value.name.trim(),
      description: form.value.description.trim(),
      template: form.value.template,
      parent_id: form.value.parentId || null,
      chunk_params: { ...params.value },
    })
    if (files.value.length) {
      const up = await uploadKgDocuments(kb.id, files.value)
      const failed = up.filter((r) => !r.ok)
      const okCount = up.length - failed.length
      if (okCount) {
        // 有成功上传的文件就触发导入(部分失败不拖累成功文件);
        // 失败明细 toast 告知,用户可在文档导入页补传
        await importKgAll(kb.id)
        message.success(`知识库「${kb.name}」已创建,${okCount} 个文档已开始导入(任务中心可查进度)`)
      } else {
        message.warning(`已建库,但 ${failed.length} 个文件上传失败:${failed.map((f) => `${f.filename}(${f.reason})`).join('; ')}`)
      }
      if (failed.length) {
        message.warning(`上传失败 ${failed.length} 个:${failed.map((f) => f.filename).join('、')}(可建库后在「文档导入」页重传)`)
      }
    } else {
      message.success(`知识库「${kb.name}」已创建`)
    }
    open.value = false
    emit('created', kb.id)
  })
  creating.value = false
}

function show() {
  open.value = true
}
defineExpose({ show })
</script>

<style scoped>
.wiz-footer { display: flex; gap: 8px; margin-top: 16px; padding-top: 12px; border-top: 1px solid #eef1f7; }
.wiz-summary .wiz-row { margin: 6px 0; font-size: 13px; color: #4b5563; }
.wiz-summary b { color: #1f2937; margin-right: 10px; }
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
.dm-upload-icon { font-size: 26px; color: #2f54eb; margin: 4px 0; }
.dm-upload-text { font-size: 13px; color: #374151; margin: 0; }
.dm-upload-hint { font-size: 12px; color: #9ca3af; margin: 2px 0 0; }
</style>
