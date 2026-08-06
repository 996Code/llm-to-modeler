<!--
  =============================================================================
  组件职责：聊天输入框（带图片上传）
  -----------------------------------------------------------------------------
  设计模式：受控组件 + 事件上抛。
    - 自身维护文本/图片的本地草稿状态；
    - 用户点发送时，通过 emit('send', text, imageBase64) 把内容上抛给父组件，
      由父组件决定如何处理（父组件 = 控制器）。
  Java 类比：相当于一个表单输入 JSP 片段，本身不持久化数据，
            通过 form submit 把数据交给后端 Controller。

  这里不直接调 store，而是 emit 事件，遵循"单向数据流"：
    父 → 子（props 传状态），子 → 父（emit 事件）。这样组件可复用、可测试。
  =============================================================================
-->
<template>
  <div class="chat-input-wrap">
    <!-- 输入框主体；:class 动态加 disabled 类（流式生成时变灰） -->
    <div class="chat-input" :class="{ disabled: streaming }">
      <!-- 图片预览：选了图才显示，v-if 条件渲染 -->
      <div v-if="imagePreview" class="image-preview">
        <!-- :src 绑定图片地址（这里用 base64 data url） -->
        <img :src="imagePreview" alt="preview" />
        <!-- 删除图片按钮；:disabled 流式生成时禁用 -->
        <button class="image-remove" @click="removeImage" :disabled="streaming">×</button>
      </div>
      <!--
        a-textarea：Ant Design 的多行输入框。
        v-model:value：双向绑定（输入框值 ↔ text 变量），类比 Java Bean 的 Property + Listener。
        :auto-size：自适应高度，最少 1 行、最多 4 行。
        @press-enter：回车键事件。
      -->
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
        <!-- 流式生成时显示提示 -->
        <span v-if="streaming" class="input-hint">生成中...</span>
        <!-- 附件（图片）按钮，点击触发隐藏的 file input -->
        <button
          class="attach-btn"
          :disabled="streaming"
          @click="triggerFileInput"
          title="上传图片"
        >
          <PaperClipOutlined />
        </button>
        <!-- 隐藏的文件选择 input（实际由按钮触发，实现自定义样式） -->
        <input
          ref="fileInputRef"
          type="file"
          accept="image/*"
          class="file-input-hidden"
          @change="onFileSelected"
        />
        <!--
          发送按钮：
          :class="{ active: canSend }" —— 有内容时高亮
          :disabled="!canSend" —— 空内容时禁用
          Loading：流式生成中显示转圈图标
        -->
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
// ref：响应式基本值；computed：派生值；watch：监听变化（类比 PropertyChangeListener）
import { ref, computed, watch } from 'vue'
import { SendOutlined, LoadingOutlined, PaperClipOutlined } from '@ant-design/icons-vue'

// defineProps：声明父组件传入的属性（接口契约，类比 Java 方法入参）。
// 泛型对象写法（<{...}>）提供类型推导。
const props = defineProps<{
  streaming: boolean                  // 是否正在流式生成（生成时禁用输入）
  pendingClarification?: boolean      // 当前是否处于追问态（影响占位文案）
}>()

// defineEmits：声明本组件会触发的事件（接口契约，类比 Java 的事件总线/回调接口）。
// 语法 send: [text, imageBase64?] 表示事件名为 send，带两个参数。
const emit = defineEmits<{
  send: [text: string, imageBase64?: string]
}>()

// ---------- 本地草稿状态（响应式）----------
const text = ref('')                            // 输入框文本
const textareaRef = ref<any>(null)              // textarea 组件实例引用（用于 focus）
const fileInputRef = ref<HTMLInputElement | null>(null)  // 隐藏 file input 的 DOM 引用
const imageBase64 = ref<string | null>(null)    // 已选图片的 base64（不含 data: 前缀）
const imagePreview = ref<string | null>(null)   // 图片预览用的 data url（含前缀）

// 是否可发送：有文本或图片，且非流式生成中。
// 【逻辑】trim() 去空格后有内容、或已选图片，二者满足其一即可发送；
//        但流式生成中（streaming）一律禁止发送，避免并发请求。
const canSend = computed(() => {
  return (text.value.trim() || imageBase64.value) && !props.streaming
})

// 占位文案：追问态与常态不同（驱动输入框 placeholder 切换）。
// 【追问态】后端抛出了需要用户补充的问题，提示用户继续输入答案。
const placeholderText = computed(() => {
  if (props.pendingClarification) {
    return '请补充以上信息，Enter 发送...'
  }
  return '描述你的需求，Enter 发送，Shift+Enter 换行...'
})

// 追问时自动聚焦输入框
// watch(监听源, 回调)：监听值变化，类比 Java 的 PropertyChangeListener.propertyChange()
watch(() => props.pendingClarification, (val) => {
  if (val) {
    // 延迟 100ms 再聚焦，等 DOM 更新完毕（避免聚焦失败）
    setTimeout(() => {
      textareaRef.value?.focus()
    }, 100)
  }
})

/** 触发文件选择：点击隐藏 input 模拟原生点击 */
function triggerFileInput() {
  fileInputRef.value?.click()
}

/**
 * 文件选中回调：校验类型/大小后读取为 base64。
 * @param e change 事件
 */
function onFileSelected(e: Event) {
  // as 是 TS 强制类型转换（类比 Java 的 (HTMLInputElement) cast）
  const input = e.target as HTMLInputElement
  // input.files?.[0]：可选链取第一个文件（?. 防 null）
  const file = input.files?.[0]
  if (!file) return

  // 验证文件类型：必须是 image/* 开头（非图片直接忽略）
  if (!file.type.startsWith('image/')) {
    return
  }

  // 限制文件大小 (10MB)，超过直接忽略
  if (file.size > 10 * 1024 * 1024) {
    return
  }

  // 用 FileReader 异步读为 data url（base64）
  // 【流程】readAsDataURL 触发异步读取 → 完成后触发 onload → 拿到 base64
  const reader = new FileReader()
  reader.onload = (ev) => {
    const dataUrl = ev.target?.result as string
    // 提取 base64 部分 (去掉 data:image/xxx;base64, 前缀)
    // 【原因】传给后端只要纯 base64，不带 MIME 前缀
    imageBase64.value = dataUrl.split(',')[1]
    // 预览则保留完整 data url（<img src> 需要）
    imagePreview.value = dataUrl
  }
  reader.readAsDataURL(file)

  // 清空 input 以允许重复选择同一文件
  // （不清空的话，选同一个文件不会再次触发 change，因为 value 没变）
  input.value = ''
}

/** 移除已选图片 */
function removeImage() {
  imageBase64.value = null
  imagePreview.value = null
}

/** 发送：把内容上抛给父组件，并清空本地草稿 */
function send() {
  // 双重保险：canSend 为假时直接退出（防止按钮被绕过）
  if (!canSend.value) return
  // 图片可能为 null，统一转 undefined（后端按可选参数处理）
  const img = imageBase64.value || undefined
  // emit 触发事件，父组件通过 @send 监听
  // emit 类比 Java 的事件总线发布：fireEvent(new SendEvent(text, img))
  emit('send', text.value.trim(), img)
  // 清空草稿（文本 + 图片），为下一次输入做准备
  text.value = ''
  imageBase64.value = null
  imagePreview.value = null
}

/**
 * 回车键处理：Enter 发送，Shift+Enter 换行。
 * @param e 键盘事件
 */
function onEnter(e: KeyboardEvent) {
  // 按住 Shift 直接 return（保留默认换行行为）
  if (e.shiftKey) return // allow newlines with Shift+Enter
  // preventDefault 阻止默认行为（回车本来会换行，这里改成发送）
  e.preventDefault()
  send()
}
</script>

<style scoped>
.chat-input-wrap {
  padding: 12px 24px 16px;
  background: var(--bg-page);
}
/* 输入框容器：最大宽度居中、横向排列、圆角、阴影 */
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
/* 聚焦时（focus-within：子元素获焦也算）高亮边框 */
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
/* 图片右上角的删除小圆点 */
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

/* textarea 自身无边框，融入容器 */
.input-box {
  flex: 1;
  border: none !important;
  box-shadow: none !important;
  padding: 6px 0 !important;
  font-size: 14px;
  resize: none;
  background: transparent;
}
/* :deep() 穿透 scoped 作用域，改子组件内部样式（这里改 textarea 原生元素） */
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
/* 附件按钮 */
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
/* 隐藏的真实 file input */
.file-input-hidden {
  display: none;
}
/* 发送按钮 */
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
/* 有内容时高亮（active 类由 :class 绑定） */
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
/* 转圈动画 */
.loading-icon {
  animation: rotate 1s linear infinite;
}
@keyframes rotate {
  to { transform: rotate(360deg); }
}
/* 底部免责提示 */
.input-footer {
  max-width: 880px;
  margin: 8px auto 0;
  text-align: center;
  font-size: 11px;
  color: var(--text-placeholder);
}

/* 窄屏调整内边距 */
@media (max-width: 768px) {
  .chat-input-wrap { padding: 8px 12px 12px; }
}
</style>
