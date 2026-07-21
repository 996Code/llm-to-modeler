<template>
  <div class="chat-input-wrap">
    <div class="chat-input" :class="{ disabled: streaming }">
      <!-- 图片预览 -->
      <div v-if="imagePreview" class="image-preview">
        <img :src="imagePreview" alt="preview" />
        <button class="image-remove" @click="removeImage" :disabled="streaming">×</button>
      </div>
      <a-textarea
        v-model:value="text"
        :placeholder="placeholderText"
        :auto-size="{ minRows: 1, maxRows: 4 }"
        @press-enter="onEnter"
        :disabled="streaming"
        class="input-box"
        :bordered="false"
        ref="textareaRef"
      />
      <div class="input-actions">
        <span v-if="streaming" class="input-hint">生成中...</span>
        <button
          class="attach-btn"
          :disabled="streaming"
          @click="triggerFileInput"
          title="上传图片"
        >
          <PaperClipOutlined />
        </button>
        <input
          ref="fileInputRef"
          type="file"
          accept="image/*"
          class="file-input-hidden"
          @change="onFileSelected"
        />
        <button
          class="send-btn"
          :class="{ active: canSend }"
          :disabled="!canSend"
          @click="send"
        >
          <LoadingOutlined v-if="streaming" class="loading-icon" />
          <SendOutlined v-else />
        </button>
      </div>
    </div>
    <div class="input-footer">
      <span>内容由 AI 生成，请核对后再使用</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { SendOutlined, LoadingOutlined, PaperClipOutlined } from '@ant-design/icons-vue'

const props = defineProps<{
  streaming: boolean
  pendingClarification?: boolean
}>()

const emit = defineEmits<{
  send: [text: string, imageBase64?: string]
}>()
const text = ref('')
const textareaRef = ref<any>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const imageBase64 = ref<string | null>(null)
const imagePreview = ref<string | null>(null)

const canSend = computed(() => {
  return (text.value.trim() || imageBase64.value) && !props.streaming
})

const placeholderText = computed(() => {
  if (props.pendingClarification) {
    return '请补充以上信息，Enter 发送...'
  }
  return '描述你的需求，Enter 发送，Shift+Enter 换行...'
})

// 追问时自动聚焦输入框
watch(() => props.pendingClarification, (val) => {
  if (val) {
    setTimeout(() => {
      textareaRef.value?.focus()
    }, 100)
  }
})

function triggerFileInput() {
  fileInputRef.value?.click()
}

function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  // 验证文件类型
  if (!file.type.startsWith('image/')) {
    return
  }

  // 限制文件大小 (10MB)
  if (file.size > 10 * 1024 * 1024) {
    return
  }

  const reader = new FileReader()
  reader.onload = (ev) => {
    const dataUrl = ev.target?.result as string
    // 提取 base64 部分 (去掉 data:image/xxx;base64, 前缀)
    imageBase64.value = dataUrl.split(',')[1]
    imagePreview.value = dataUrl
  }
  reader.readAsDataURL(file)

  // 清空 input 以允许重复选择同一文件
  input.value = ''
}

function removeImage() {
  imageBase64.value = null
  imagePreview.value = null
}

function send() {
  if (!canSend.value) return
  const img = imageBase64.value || undefined
  emit('send', text.value.trim(), img)
  text.value = ''
  imageBase64.value = null
  imagePreview.value = null
}

function onEnter(e: KeyboardEvent) {
  if (e.shiftKey) return // allow newlines with Shift+Enter
  e.preventDefault()
  send()
}
</script>

<style scoped>
.chat-input-wrap {
  padding: 12px 24px 16px;
  background: var(--bg-page);
}
.chat-input {
  max-width: 880px;
  margin: 0 auto;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 8px 8px 8px 16px;
  background: var(--bg-container);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  transition: border-color 0.2s, box-shadow 0.2s;
}
.chat-input:focus-within {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-input);
}
.chat-input.disabled { opacity: 0.8; }

/* 图片预览 */
.image-preview {
  position: relative;
  margin: 4px 0 0 0;
  flex-shrink: 0;
}
.image-preview img {
  max-width: 120px;
  max-height: 80px;
  border-radius: var(--radius-md);
  object-fit: cover;
  border: 1px solid var(--border-color-light);
}
.image-remove {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: none;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
  transition: background 0.2s;
}
.image-remove:hover {
  background: rgba(0, 0, 0, 0.8);
}
.image-remove:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.input-box {
  flex: 1;
  border: none !important;
  box-shadow: none !important;
  padding: 6px 0 !important;
  font-size: 14px;
  resize: none;
  background: transparent;
}
.input-box :deep(textarea) {
  padding: 0 !important;
  border: none !important;
  resize: none;
}
.input-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.input-hint {
  font-size: 12px;
  color: var(--text-secondary);
}
.attach-btn {
  width: 34px; height: 34px;
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  flex-shrink: 0;
}
.attach-btn:hover {
  color: var(--color-primary);
  background: var(--color-primary-bg);
}
.attach-btn:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}
.file-input-hidden {
  display: none;
}
.send-btn {
  width: 34px; height: 34px;
  border-radius: var(--radius-md);
  border: none;
  background: var(--border-color);
  color: #fff;
  font-size: 15px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  flex-shrink: 0;
}
.send-btn.active {
  background: var(--color-primary);
}
.send-btn.active:hover {
  background: var(--color-primary-hover);
  transform: scale(1.05);
}
.send-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.loading-icon {
  animation: rotate 1s linear infinite;
}
@keyframes rotate {
  to { transform: rotate(360deg); }
}
.input-footer {
  max-width: 880px;
  margin: 8px auto 0;
  text-align: center;
  font-size: 11px;
  color: var(--text-placeholder);
}

@media (max-width: 768px) {
  .chat-input-wrap { padding: 8px 12px 12px; }
}
</style>
