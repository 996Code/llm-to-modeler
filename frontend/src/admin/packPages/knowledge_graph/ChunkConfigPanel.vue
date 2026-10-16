<template>
  <div class="ccp">
    <!-- 左:分段参数旋钮;右:块预览(调参即时看效果) -->
    <div class="ccp-grid">
      <div class="ccp-params">
        <div class="ccp-params-title">分段参数</div>
        <div class="ccp-row">
          <span class="ccp-label">目标块字数</span>
          <a-input-number v-model:value="p.chunk_target_chars" :min="200" :max="8000"
                          :step="100" size="small" style="width: 110px" />
        </div>
        <div class="ccp-row">
          <span class="ccp-label">块间重叠字数</span>
          <a-input-number v-model:value="p.chunk_overlap_chars" :min="0" :max="500"
                          :step="10" size="small" style="width: 110px" />
        </div>
        <div class="ccp-row">
          <span class="ccp-label">单块硬上限</span>
          <a-input-number v-model:value="p.chunk_max_chars" :min="500" :max="16000"
                          :step="100" size="small" style="width: 110px" />
        </div>
        <div class="ccp-row">
          <span class="ccp-label">章节块上限</span>
          <a-input-number v-model:value="p.chunk_structural_max_chars" :min="1000" :max="50000"
                          :step="1000" size="small" style="width: 110px" />
        </div>
        <div class="ccp-row">
          <span class="ccp-label">向量子切块字数</span>
          <a-input-number v-model:value="p.vector_subchunk_chars" :min="300" :max="4000"
                          :step="100" size="small" style="width: 110px" />
        </div>
        <div class="ccp-note">
          <b>抽取按章、召回按段</b>:识别到章节标题整章一块(上下文完整);
          向量化前再按句读子切(向量不稀释);检索命中子段后回带所属
          整章前后各 ~2000 字上下文发给 AI(父子回带)。
        </div>
      </div>

      <div class="ccp-preview">
        <div class="ccp-params-title">
          块预览
          <a-select v-if="files.length > 1" v-model:value="previewIdx" size="small"
                    style="width: 180px; margin-left: 8px">
            <a-select-option v-for="(f, i) in files" :key="i" :value="i">{{ f.name }}</a-select-option>
          </a-select>
        </div>
        <div v-if="!files.length" class="ccp-empty">上一步选择的文件将在此预览切块效果</div>
        <template v-else>
          <div v-if="loading" class="ccp-empty">切块预览中…</div>
          <template v-else-if="result">
            <div class="ccp-stats">
              <a-tag color="blue">{{ result.stats.chunkCount }} 块</a-tag>
              <a-tag color="geekblue">{{ result.stats.subChunkTotal }} 向量段</a-tag>
              <a-tag>{{ result.stats.minChars }}~{{ result.stats.maxChars }} 字/块</a-tag>
              <a-tag v-if="result.stats.chunkCount" color="cyan">平均 {{ result.stats.avgChars }} 字</a-tag>
            </div>
            <div class="ccp-chunk-list">
              <div v-for="c in result.chunks.slice(0, 30)" :key="c.seq" class="ccp-chunk">
                <span class="ccp-chunk-seq">#{{ c.seq }}</span>
                <span class="ccp-chunk-meta">{{ c.charCount }} 字 · {{ c.subChunks }} 段</span>
                <span class="ccp-chunk-text">{{ c.preview }}</span>
              </div>
              <div v-if="result.chunks.length > 30" class="ccp-empty">
                … 其余 {{ result.chunks.length - 30 }} 块略
              </div>
            </div>
          </template>
          <div v-else-if="error" class="ccp-empty ccp-error">{{ error }}</div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// D33 建库向导:分段配置面板(参数旋钮 + 实时预览)。
// 预览走后端 chunk-preview 端点——与导入同一条切块代码路径,
// 预览看到的块 = 导入落库的块(不骗人)。向导步骤②与已有库的
// 分段重配弹窗共用本组件。
import { computed, ref, watch } from 'vue'
import { KgChunkParams, KgChunkPreviewResult, kgChunkPreview } from '../../api'

const props = defineProps<{
  modelValue: Required<KgChunkParams>
  files: File[]
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: Required<KgChunkParams>): void }>()

const p = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const previewIdx = ref(0)
const loading = ref(false)
const result = ref<KgChunkPreviewResult | null>(null)
const error = ref('')
let seq = 0   // 请求序号守卫:旧预览晚到不得覆盖新参数的结果
let timer: ReturnType<typeof setTimeout> | undefined

// 调参/换文件 → 防抖 400ms 重预览(拖动数字框不逐击请求)
watch(
  () => [props.modelValue, previewIdx.value, props.files.length] as const,
  () => {
    const file = props.files[previewIdx.value]
    if (!file) { result.value = null; return }
    const mySeq = ++seq
    clearTimeout(timer)
    timer = setTimeout(async () => {
      loading.value = true
      error.value = ''
      try {
        const r = await kgChunkPreview(file, props.modelValue)
        if (mySeq === seq) result.value = r
      } catch (e: any) {
        if (mySeq === seq) {
          result.value = null
          error.value = e?.response?.data?.detail || e?.message || '预览失败'
        }
      } finally {
        if (mySeq === seq) loading.value = false
      }
    }, 400)
  },
  { immediate: true, deep: true },
)

// 文件列表变化时钳制预览下标
watch(() => props.files.length, (n) => {
  if (previewIdx.value >= n) previewIdx.value = Math.max(0, n - 1)
})
</script>

<style scoped>
.ccp-grid { display: grid; grid-template-columns: 300px 1fr; gap: 14px; }
.ccp-params, .ccp-preview {
  border: 1px solid #eef1f7; border-radius: 10px; padding: 10px 14px; background: #fff;
}
.ccp-params-title { font-weight: 600; font-size: 13px; color: #1f2937; margin-bottom: 8px; display: flex; align-items: center; }
.ccp-row { display: flex; align-items: center; justify-content: space-between; margin: 6px 0; }
.ccp-label { font-size: 12.5px; color: #4b5563; }
.ccp-note {
  margin-top: 10px; padding: 8px 10px; background: #f8fafc; border-radius: 8px;
  font-size: 12px; color: #6b7280; line-height: 1.7;
}
.ccp-empty { color: #9ca3af; font-size: 12.5px; padding: 24px 0; text-align: center; }
.ccp-error { color: #ef4444; }
.ccp-stats { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.ccp-chunk-list { max-height: 320px; overflow-y: auto; }
.ccp-chunk {
  display: flex; gap: 8px; align-items: baseline; padding: 4px 0;
  border-bottom: 1px dashed #f0f0f0; font-size: 12px;
}
.ccp-chunk-seq { color: #2f54eb; font-weight: 600; flex-shrink: 0; }
.ccp-chunk-meta { color: #94a3b8; flex-shrink: 0; }
.ccp-chunk-text {
  color: #4b5563; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
