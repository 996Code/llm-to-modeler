<template>
  <div class="chat-panel">
    <!-- Messages -->
    <div class="message-list" ref="msgListRef">
      <!-- Welcome screen -->
      <div v-if="!store.messages.length && !store.streaming" class="welcome">
        <div class="welcome-logo">
          <FormOutlined />
        </div>
        <h1 class="welcome-title">表单配置助手</h1>
        <p class="welcome-subtitle">描述你想要的表单，我来生成完整的低码配置</p>
        <div class="examples">
          <div
            v-for="ex in examples"
            :key="ex.title"
            class="example-card"
            @click="quickFill(ex.prompt)"
          >
            <component :is="ex.icon" class="example-icon" />
            <div class="example-text">
              <div class="example-title">{{ ex.title }}</div>
              <div class="example-desc">{{ ex.desc }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Chat messages -->
      <template v-for="(msg, i) in store.messages" :key="i">
        <!-- User message (right-aligned bubble) -->
        <div v-if="msg.role === 'user'" class="message-row user-row">
          <div class="msg-bubble user-bubble">{{ msg.content }}</div>
          <div class="msg-avatar user-avatar">
            <UserOutlined />
          </div>
        </div>

        <!-- Assistant message (left-aligned, no bubble) -->
        <div v-else class="message-row assistant-row">
          <div class="msg-avatar assistant-avatar">
            <FormOutlined />
          </div>
          <div class="msg-body">
            <div class="msg-text">{{ msg.content }}</div>
            
            <!-- Clarification questions -->
            <div v-if="msg.needsClarification && msg.clarificationQuestions" class="clarification-card">
              <div class="clarification-header">
                <QuestionCircleOutlined class="clarification-icon" />
                <span>需要确认以下信息</span>
              </div>
              <ul class="clarification-questions">
                <li v-for="(q, idx) in msg.clarificationQuestions" :key="idx">
                  {{ q }}
                </li>
              </ul>
              <div class="clarification-hint">
                请补充以上信息后，我会继续为您生成配置
              </div>
            </div>
            
            <!-- Config card -->
            <div
              v-if="msg.configSnapshot"
              class="config-card"
              @click="selectConfig(msg.configSnapshot)"
            >
              <div class="config-card-header">
                <TableOutlined class="card-icon" />
                <span class="card-title">{{ msg.configSnapshot.formName || '表单配置' }}</span>
                <a-tag color="success" class="card-tag">
                  <CheckCircleOutlined /> 已校验
                </a-tag>
              </div>
              <div class="config-card-body">
                <div class="card-stat">
                  <span class="stat-num">{{ msg.configSnapshot.formFieldConfigVos?.length || 0 }}</span>
                  <span class="stat-label">个字段</span>
                </div>
                <div class="card-stat">
                  <span class="stat-num">{{ msg.configSnapshot.formColumnsNumber }}</span>
                  <span class="stat-label">列布局</span>
                </div>
              </div>
              <div class="config-card-actions">
                <a-button size="small" type="link" @click.stop="showJsonViewer(msg.configSnapshot)">
                  <EyeOutlined /> 查看 JSON
                </a-button>
                <a-button v-if="embedded" size="small" type="primary" @click.stop="applyConfig(msg.configSnapshot)">
                  <CheckOutlined /> 应用配置
                </a-button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- Pipeline progress (streaming) -->
      <div v-if="store.streaming" class="pipeline-card">
        <div class="pipeline-header">
          <div class="thinking-dots">
            <span></span><span></span><span></span>
          </div>
          <span class="pipeline-title">{{ store.stageMessage || '正在思考...' }}</span>
        </div>
        <div class="pipeline-steps">
          <div
            v-for="step in pipelineSteps"
            :key="step.key"
            class="pipeline-step"
            :class="step.status"
          >
            <div class="step-indicator">
              <CheckOutlined v-if="step.status === 'done'" />
              <span v-else-if="step.status === 'active'" class="step-pulse"></span>
              <span v-else class="step-num">{{ step.index }}</span>
            </div>
            <span class="step-label">{{ step.label }}</span>
          </div>
        </div>
      </div>
    </div>

    <ChatInput
      @send="handleSend"
      :streaming="store.streaming"
      :pending-clarification="store.pendingClarification"
    />

    <!-- JSON 查看器 Modal -->
    <Modal
      v-model:open="jsonViewerVisible"
      title="表单配置 JSON"
      :footer="null"
      width="80%"
      style="top: 20px"
    >
      <pre style="max-height: 70vh; overflow: auto; background: #f5f5f5; padding: 16px; border-radius: 4px; font-size: 12px; line-height: 1.5;">{{ jsonViewerContent }}</pre>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import {
  UserOutlined, FormOutlined, TableOutlined, CheckOutlined,
  CheckCircleOutlined, EyeOutlined, QuestionCircleOutlined,
  SolutionOutlined, TeamOutlined, ContactsOutlined,
} from '@ant-design/icons-vue'
import { Modal } from 'ant-design-vue'
import { useConversationStore } from '../../stores/conversation'
import type { FormConfig } from '../../types'
import ChatInput from './ChatInput.vue'

defineProps<{ embedded?: boolean }>()

const store = useConversationStore()
const msgListRef = ref<HTMLElement>()

// JSON 查看器状态
const jsonViewerVisible = ref(false)
const jsonViewerContent = ref('')

function showJsonViewer(config: FormConfig) {
  jsonViewerContent.value = JSON.stringify(config, null, 2)
  jsonViewerVisible.value = true
}

const examples = [
  {
    title: '请假申请表',
    desc: '申请人、请假类型、日期范围',
    prompt: '创建一个请假申请表，包含申请人、请假类型、开始日期、结束日期',
    icon: SolutionOutlined,
  },
  {
    title: '员工信息表',
    desc: '姓名、部门、手机号、入职日期',
    prompt: '创建一个员工信息表，包含姓名、部门、手机号、入职日期',
    icon: TeamOutlined,
  },
  {
    title: '联系人表单',
    desc: '姓名、邮箱、电话、地址',
    prompt: '创建一个联系人表单，包含姓名、邮箱、电话、地址',
    icon: ContactsOutlined,
  },
  {
    title: '客户反馈表',
    desc: '客户名称、反馈类型、详细描述',
    prompt: '创建一个客户反馈表，包含客户名称、反馈类型（下拉选择）、详细描述',
    icon: FormOutlined,
  },
]

// Pipeline step tracking
// CREATE pipeline: 7 steps (classify_intent → fetch_guide → list_assets → parse_fields → fetch_templates → generate → validate)
// MODIFY pipeline: 4 steps (classify_intent → fetch_guide → modify → validate)
const CREATE_STAGES = [
  { key: 'classify_intent', label: '理解意图' },
  { key: 'fetch_guide', label: '获取指南' },
  { key: 'list_assets', label: '加载模板' },
  { key: 'parse_fields', label: '解析字段' },
  { key: 'fetch_templates', label: '匹配模板' },
  { key: 'generate', label: '生成配置' },
  { key: 'validate', label: '校验结果' },
]
const MODIFY_STAGES = [
  { key: 'classify_intent', label: '理解意图' },
  { key: 'fetch_guide', label: '获取指南' },
  { key: 'modify', label: '修改配置' },
  { key: 'validate', label: '校验结果' },
]

// Backend emits "completion" variants of a stage by appending a suffix:
//   list_assets_done / parse_fields_done / fetch_templates_done / generate_done
//   validate_pass / validate_fail
// The bare stage name (e.g. "generate", "generate_retry") means "in progress".
const COMPLETE_SUFFIX = /(_done|_pass|_fail)$/

// Which pipeline are we in? Modify = had a config before this message.
const isModifyMode = computed(() => store.streamingModify)

const activeStages = computed(() => isModifyMode.value ? MODIFY_STAGES : CREATE_STAGES)

const pipelineSteps = computed(() => {
  const currentStage = store.currentStage
  const stages = activeStages.value
  const result = stages.map((s, i) => {
    let status: 'pending' | 'active' | 'done' = 'pending'
    if (currentStage) {
      // Which pipeline step is the backend reporting? (prefix match on the stage)
      const currentIdx = stages.findIndex(x => currentStage.startsWith(x.key))
      if (currentIdx > i) {
        status = 'done'             // a later step is running → this one finished
      } else if (currentIdx === i) {
        // bare name (e.g. "generate", "generate_retry") = active;
        // suffixed name (e.g. "generate_done", "validate_pass") = done
        status = COMPLETE_SUFFIX.test(currentStage) ? 'done' : 'active'
      }
    }
    return { ...s, index: i + 1, status }
  })
  // If streaming done with config, all steps done
  if (!store.streaming && store.stageMessage === '' && store.currentConfig) {
    return result.map(s => ({ ...s, status: 'done' }))
  }
  return result
})

function quickFill(text: string) {
  store.sendMessage(text)
}

function handleSend(text: string) {
  // 统一入口：后端自动识别意图（create/modify/general）
  store.sendMessage(text)
}

function selectConfig(config: FormConfig) {
  store.currentConfig = config
}

function applyConfig(config: FormConfig) {
  // 深拷贝为纯 JSON，避免 Vue 响应式代理对象导致 DataCloneError
  const plainConfig = JSON.parse(JSON.stringify(config))
  window.parent.postMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config: plainConfig } }, '*')
}

watch(() => store.messages.length, () => {
  nextTick(() => {
    if (msgListRef.value) msgListRef.value.scrollTop = msgListRef.value.scrollHeight
  })
})

watch(() => store.stageMessage, () => {
  nextTick(() => {
    if (msgListRef.value) msgListRef.value.scrollTop = msgListRef.value.scrollHeight
  })
})
</script>

<style scoped>
.chat-panel { flex: 1; display: flex; flex-direction: column; min-height: 0; }

.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 24px 0;
}

/* ===== Welcome screen ===== */
.welcome {
  max-width: 720px;
  margin: 0 auto;
  padding: 80px 24px 40px;
  text-align: center;
}
.welcome-logo {
  width: 64px; height: 64px;
  margin: 0 auto 20px;
  border-radius: var(--radius-xl);
  background: linear-gradient(135deg, var(--color-primary), #5b8cff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 30px;
  box-shadow: 0 8px 24px rgba(51, 112, 255, 0.25);
}
.welcome-title {
  font-size: 28px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 10px;
}
.welcome-subtitle {
  font-size: 15px;
  color: var(--text-secondary);
  margin-bottom: 40px;
}
.examples {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  text-align: left;
}
.example-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--border-color-light);
  border-radius: var(--radius-lg);
  cursor: pointer;
  background: var(--bg-container);
  transition: all 0.2s;
}
.example-card:hover {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-input);
  transform: translateY(-1px);
}
.example-icon {
  font-size: 20px;
  color: var(--color-primary);
  margin-top: 2px;
  flex-shrink: 0;
}
.example-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
  margin-bottom: 3px;
}
.example-desc {
  font-size: 12px;
  color: var(--text-secondary);
}

/* ===== Messages ===== */
.message-row {
  max-width: 880px;
  margin: 0 auto 24px;
  padding: 0 24px;
  display: flex;
  gap: 12px;
}
.user-row {
  flex-direction: row-reverse;
}
.msg-avatar {
  width: 34px; height: 34px;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}
.user-avatar {
  background: var(--color-primary);
  color: #fff;
}
.assistant-avatar {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid #d6e1ff;
}

.user-row .msg-body { display: none; }

.msg-bubble {
  padding: 10px 14px;
  border-radius: var(--radius-lg);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
  max-width: 75%;
}
.user-bubble {
  background: var(--color-primary);
  color: #fff;
  border-top-right-radius: var(--radius-sm);
}
.msg-body {
  flex: 1;
  min-width: 0;
  padding-top: 4px;
}
.msg-text {
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-primary);
  word-break: break-word;
  background: var(--bg-container);
  padding: 12px 16px;
  border-radius: var(--radius-lg);
  border-top-left-radius: var(--radius-sm);
  border: 1px solid var(--border-color-lighter);
}

/* ===== Config card ===== */
.config-card {
  margin-top: 10px;
  border: 1px solid var(--border-color-light);
  border-radius: var(--radius-lg);
  background: var(--bg-container);
  overflow: hidden;
  cursor: pointer;
  transition: all 0.2s;
}

/* ===== Clarification card ===== */
.clarification-card {
  margin-top: 10px;
  border: 1px solid var(--color-warning, #faad14);
  border-radius: var(--radius-lg);
  background: #fffbe6;
  padding: 16px;
}
.clarification-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-weight: 500;
  color: var(--text-primary);
}
.clarification-icon {
  color: var(--color-warning, #faad14);
  font-size: 18px;
}
.clarification-questions {
  margin: 0;
  padding-left: 20px;
  color: var(--text-regular);
  line-height: 1.8;
}
.clarification-questions li {
  margin-bottom: 4px;
}
.clarification-hint {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(250, 173, 20, 0.2);
  font-size: 13px;
  color: var(--text-secondary);
  font-style: italic;
}
.config-card:hover {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-md);
}
.config-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color-lighter);
  background: linear-gradient(to right, var(--color-primary-bg), transparent);
}
.card-icon { color: var(--color-primary); font-size: 16px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--text-primary); flex: 1; }
.card-tag { margin-left: auto !important; border: none !important; background: #e8f9f0 !important; color: var(--color-success) !important; font-size: 11px; padding: 2px 8px !important; }
.config-card-body {
  display: flex;
  gap: 32px;
  padding: 14px 16px;
}
.card-stat { display: flex; align-items: baseline; gap: 4px; }
.stat-num { font-size: 22px; font-weight: 600; color: var(--color-primary); }
.stat-label { font-size: 12px; color: var(--text-secondary); }
.config-card-actions {
  display: flex;
  gap: 8px;
  padding: 0 16px 12px;
}

/* ===== Pipeline progress ===== */
.pipeline-card {
  max-width: 880px;
  margin: 0 auto 24px;
  padding: 0 24px;
}
.pipeline-card .pipeline-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  background: var(--bg-container);
  border: 1px solid var(--border-color-lighter);
  border-radius: var(--radius-lg);
  padding: 14px 18px;
}
.thinking-dots {
  display: flex;
  gap: 4px;
}
.thinking-dots span {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: thinking 1.2s infinite ease-in-out;
}
.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes thinking {
  0%, 60%, 100% { opacity: 0.3; transform: scale(0.8); }
  30% { opacity: 1; transform: scale(1); }
}
.pipeline-title { font-size: 13px; color: var(--text-primary); font-weight: 500; }
.pipeline-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.pipeline-step {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 16px;
  background: var(--bg-container);
  border: 1px solid var(--border-color-lighter);
  transition: all 0.3s;
}
.step-indicator {
  width: 16px; height: 16px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  flex-shrink: 0;
}
.step-num {
  color: var(--text-placeholder);
  font-weight: 600;
}
.pipeline-step.done {
  border-color: #c6edd9;
  background: #f0fcf5;
}
.pipeline-step.done .step-indicator { background: var(--color-success); color: #fff; }
.pipeline-step.done .step-label { color: var(--color-success); }
.pipeline-step.active {
  border-color: #b3cfff;
  background: var(--color-primary-bg);
}
.pipeline-step.active .step-indicator {
  background: var(--color-primary);
  position: relative;
}
.step-pulse {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #fff;
  animation: pulse 1s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.7); }
}
.pipeline-step.active .step-label { color: var(--color-primary); font-weight: 500; }
.pipeline-step.pending .step-indicator { background: #e8e9eb; }
.step-label { font-size: 12px; color: var(--text-secondary); }

@media (max-width: 768px) {
  .examples { grid-template-columns: 1fr; }
  .message-row { padding: 0 12px; }
  .pipeline-card { padding: 0 12px; }
}
</style>
