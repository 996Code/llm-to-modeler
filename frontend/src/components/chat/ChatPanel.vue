<!--
  ChatPanel.vue —— 聊天主面板组件
  -----------------------------------------------------------------------------
  【模块职责】
  这是整个应用的核心交互区域：负责渲染欢迎页、消息气泡、配置卡片、数据卡片、
  追问卡片、以及流式生成过程中的 pipeline 进度条。同时内嵌了 ChatInput 输入框。

  【类比 Java】
  相当于一个 JSP/Thymeleaf 视图组件 + Controller 的合体：
    - <template> 部分类似 JSP 视图，负责声明式渲染；
    - <script setup> 部分类似 Controller，持有状态和事件处理逻辑。

  【Vue 核心概念速查（给 Java 同学）】
    - ref(value)         声明响应式变量，类比 ObservableValue；改 .value 触发视图刷新
    - computed(() => …)  声明派生值（带缓存），类比 Java 的 getter
    - watch(source, cb)  监听变化，类比 PropertyChangeListener
    - store.xxx          来自 Pinia 的全局状态，类比 @Autowired 注入的 @Service 单例
    - @click / @press-enter  事件绑定，类比 Swing 的 addActionListener
-->
<template>
  <div class="chat-panel">
    <!-- ===== 消息列表区域（自动滚动） ===== -->
    <!-- ref="msgListRef"：把 DOM 引用绑定到 script 中的 msgListRef，用于手动控制滚动位置 -->
    <div class="message-list" ref="msgListRef">
      <!-- ===== 欢迎屏：仅在「无消息 且 非流式中」时显示 ===== -->
      <!-- v-if 是条件渲染：为 false 时该 DOM 完全不存在（区别于 v-show 仅隐藏） -->
      <div v-if="!store.messages.length && !store.streaming" class="welcome">
        <div class="welcome-logo">
          <FormOutlined />
        </div>
        <h1 class="welcome-title">智能助手</h1>
        <p class="welcome-subtitle">用自然语言描述你的需求，我来帮你完成</p>
        <div class="examples">
          <!-- v-for：列表渲染（类比 Java 的 forEach）；:key 是 Vue 复用 DOM 的唯一标识 -->
          <div
            v-for="ex in examples"
            :key="ex.title"
            class="example-card"
            @click="quickFill(ex.prompt)"
          >
            <!-- 动态组件：:is 指向一个图标组件，类比 Java 的反射实例化 -->
            <component :is="ex.icon" class="example-icon" />
            <div class="example-text">
              <div class="example-title">{{ ex.title }}</div>
              <div class="example-desc">{{ ex.desc }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== 消息气泡列表：遍历 store 中的所有消息 ===== -->
      <!-- 用 <template v-for> 包裹：因为每条消息要根据 role 渲染成不同结构 -->
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
            
            <!-- ===== 追问卡片：当消息需要用户补充信息时显示 ===== -->
            <!-- v-if 双条件：needsClarification 标记 + 有问题列表 -->
            <div v-if="msg.needsClarification && msg.clarificationQuestions" class="clarification-card">
              <div class="clarification-header">
                <QuestionCircleOutlined class="clarification-icon" />
                <span>需要确认以下信息</span>
              </div>
              <ul class="clarification-questions">
                <li v-for="(q, idx) in msg.clarificationQuestions" :key="idx">
                  {{ typeof q === 'string' ? q : q.question }}
                </li>
              </ul>
              <div class="clarification-hint">
                请补充以上信息后，我会继续为您生成配置
              </div>
            </div>
            
            <!-- ===== 配置卡片 (artifact_type=config: 表单配置) ===== -->
            <!-- 仅当该消息携带 configSnapshot 时渲染；点击把该配置设为当前配置 -->
            <div
              v-if="msg.configSnapshot"
              class="config-card"
              @click="selectConfig(msg.configSnapshot)"
            >
              <div class="config-card-header">
                <TableOutlined class="card-icon" />
                <!-- 卡片标题：优先用后端给的 formName/title，都没有就兜底为「配置结果」 -->
                <span class="card-title">{{ msg.formattedData?.formName || msg.formattedData?.title || '配置结果' }}</span>
                <!-- 「已校验」标签：表示该配置通过了后端校验 -->
                <a-tag color="success" class="card-tag">
                  <CheckCircleOutlined /> 已校验
                </a-tag>
              </div>
              <!-- 卡片正文：展示字段数统计（仅在后端返回了 fieldCount 时显示） -->
              <div class="config-card-body">
                <div v-if="msg.formattedData?.fieldCount !== undefined" class="card-stat">
                  <span class="stat-num">{{ msg.formattedData.fieldCount }}</span>
                  <span class="stat-label">个字段</span>
                </div>
              </div>
              <div class="config-card-actions">
                <!-- @click.stop：阻止事件冒泡（避免触发外层 config-card 的点击） -->
                <a-button size="small" type="link" @click.stop="showJsonViewer(msg.configSnapshot)">
                  <EyeOutlined /> 查看 JSON
                </a-button>
                <!-- 仅嵌入模式显示「应用配置」：把配置通过 postMessage 推给父窗口 -->
                <a-button v-if="embedded" size="small" type="primary" @click.stop="applyConfig(msg.configSnapshot)">
                  <CheckOutlined /> 应用配置
                </a-button>
              </div>
            </div>

            <!-- ===== 数据卡片 (artifact_type=data: 非配置类结果) ===== -->
            <!-- v-else-if 确保与 config-card 互斥:一条消息不会同时渲染两种卡片 -->
            <div
              v-else-if="msg.dataResult"
              class="data-card"
            >
              <div class="data-card-header">
                <CheckCircleOutlined class="card-icon data-icon" />
                <span class="card-title">{{ msg.formattedData?.title || msg.formattedData?.formName || '操作结果' }}</span>
                <a-tag color="processing" class="card-tag">
                  已完成
                </a-tag>
              </div>
              <div class="data-card-body">
                <!-- 遍历「提取后的可显示字段」（getDataDisplayFields 已做中文化与过滤） -->
                <div v-for="(value, key) in getDataDisplayFields(msg.dataResult)" :key="key" class="data-field">
                  <span class="data-field-label">{{ key }}</span>
                  <span class="data-field-value">{{ value }}</span>
                </div>
              </div>
              <div class="data-card-actions">
                <a-button size="small" type="link" @click.stop="showJsonViewer(msg.dataResult)">
                  <EyeOutlined /> 查看详情
                </a-button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- ===== Pipeline 进度卡片（流式生成中显示） ===== -->
      <!-- 仅在 store.streaming=true（后端正在处理）时渲染 -->
      <div v-if="store.streaming" class="pipeline-card">
        <div class="pipeline-header">
          <!-- 思考动画：三个圆点依次闪烁，纯视觉反馈 -->
          <div class="thinking-dots">
            <span></span><span></span><span></span>
          </div>
          <!-- 阶段提示文案（store.stageMessage），没有就兜底「正在思考...」 -->
          <span class="pipeline-title">{{ store.stageMessage || '正在思考...' }}</span>
        </div>
        <div class="pipeline-steps">
          <!-- 遍历计算后的步骤列表（含 status：pending/active/done） -->
          <div
            v-for="step in pipelineSteps"
            :key="step.key"
            class="pipeline-step"
            :class="step.status"
          >
            <!-- 步骤指示器：根据状态显示对勾/脉冲点/序号 -->
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
      title="配置 JSON"
      :footer="null"
      width="80%"
      style="top: 20px"
    >
      <pre style="max-height: 70vh; overflow: auto; background: #f5f5f5; padding: 16px; border-radius: 4px; font-size: 12px; line-height: 1.5;">{{ jsonViewerContent }}</pre>
    </Modal>
  </div>
</template>

<script setup lang="ts">
// ===== Vue Composition API 引入（类比 Java 的 import） =====
// ref/computed/watch：见文件顶部说明；nextTick：等下一次 DOM 更新后再执行（用于滚动到底部）
import { ref, computed, watch, nextTick } from 'vue'
// 图标组件（ant-design-vue 图标库，按需引入以减小打包体积）
import {
  UserOutlined, FormOutlined, TableOutlined, CheckOutlined,
  CheckCircleOutlined, EyeOutlined, QuestionCircleOutlined,
  SolutionOutlined, TeamOutlined, ContactsOutlined,
} from '@ant-design/icons-vue'
// Modal：ant-design-vue 的弹窗组件（命令式调用）
import { Modal } from 'ant-design-vue'
// 全局会话 Store（Pinia 单例），类比 @Autowired private ConversationStore store
import { useConversationStore } from '../../stores/conversation'
// 仅引入类型（编译期检查，运行时不打包）
import type { FormConfig } from '../../types'
// 子组件：输入框
import ChatInput from './ChatInput.vue'

// ===== 组件入参（props）声明 =====
// defineProps<{ embedded?: boolean }>() —— 编译宏，声明本组件接收一个可选的 embedded 标记。
// 【类比 Java】相当于接口契约：父组件传递 embedded=true 表示当前是嵌入模式。
// 嵌入模式下配置卡片会显示「应用配置」按钮（通过 postMessage 推给父窗口）。
defineProps<{ embedded?: boolean }>()

// 获取全局 store 实例（setup 中调用一次，整个组件生命周期复用）
const store = useConversationStore()
// 消息列表容器的 DOM 引用，用于手动控制 scrollTop（自动滚到底部）
const msgListRef = ref<HTMLElement>()

// JSON 查看器状态：弹窗显隐 + 弹窗内显示的 JSON 文本
const jsonViewerVisible = ref(false)
const jsonViewerContent = ref('')

/**
 * 打开 JSON 查看器弹窗：把任意对象格式化成 2 空格缩进的 JSON 字符串后展示。
 * @param data 要展示的对象（表单配置或数据结果）
 */
function showJsonViewer(data: FormConfig | Record<string, any>) {
  // JSON.stringify 第三参 2 表示缩进 2 空格（美化输出）
  jsonViewerContent.value = JSON.stringify(data, null, 2)
  jsonViewerVisible.value = true
}

/** 字段 key → 中文标签映射(常用业务字段)，用于把后端返回的英文 key 翻译成中文展示 */
const FIELD_LABEL_MAP: Record<string, string> = {
  applicant: '申请人',
  leaveType: '请假类型',
  startDate: '开始日期',
  endDate: '结束日期',
  reason: '原因',
  status: '状态',
  approvalId: '审批编号',
  name: '姓名',
  department: '部门',
  phone: '手机号',
  email: '邮箱',
  address: '地址',
  type: '类型',
  title: '标题',
  description: '描述',
  amount: '金额',
  date: '日期',
  category: '分类',
  remark: '备注',
  id: '编号',
}

/**
 * 从数据结果中提取「适合展示」的字段（最多 6 个）。
 *
 * 过滤规则：
 *  - 跳过内部字段（status / approvalId 等，它们对最终用户无意义）
 *  - 跳过复杂嵌套对象（难以在卡片里平铺展示）
 *  - 跳过空值（'' / null / undefined）
 *  - 使用 FIELD_LABEL_MAP 把 key 映射为中文标签，没有映射则保留原 key
 *
 * @param data 后端返回的数据结果对象
 * @returns { 中文标签: 值字符串 } 的有序映射
 */
function getDataDisplayFields(data: Record<string, any>): Record<string, string> {
  // Set 类比 Java 的 HashSet，用于 O(1) 判断字段是否需隐藏
  const HIDDEN_FIELDS = new Set(['status', 'approvalId'])
  const result: Record<string, string> = {}
  let count = 0
  // Object.entries 把对象转成 [key, value] 数组，便于遍历
  for (const [key, value] of Object.entries(data)) {
    if (count >= 6) break                        // 最多展示 6 个字段
    if (HIDDEN_FIELDS.has(key)) continue        // 跳过内部字段
    if (typeof value === 'object' && value !== null) continue  // 跳过嵌套对象
    if (value === '' || value === null || value === undefined) continue
    const label = FIELD_LABEL_MAP[key] || key   // 有映射用中文,没有就保留原 key
    result[label] = String(value)
    count++
  }
  return result
}

// 欢迎页示例卡片数据：点击会把 prompt 填入并发送给后端
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

// ===== Pipeline 步骤状态计算 =====
// 后端通过 SSE 推送阶段名（stage），命名约定如下：
//   - 裸名（如 "generate"、"generate_retry"）= 进行中（active）
//   - 带完成后缀（如 "generate_done"、"validate_pass"、"validate_fail"）= 已完成（done）
// 这个正则用于识别"已完成"后缀
const COMPLETE_SUFFIX = /(_done|_pass|_fail)$/

// 动态 pipeline 步骤定义（从 store 获取，由后端通过 pipeline_definition 事件下发）
const activeStages = computed(() => store.pipelineSteps)

/**
 * 计算每个 pipeline 步骤的展示状态（pending / active / done），驱动进度条 UI。
 *
 * 推算规则：
 *  1. 用后端当前上报的 currentStage 做 key 前缀匹配，定位到"正在执行"的步骤下标 currentIdx；
 *  2. 下标 < currentIdx 的步骤标记为 done（更早的已完成）；
 *  3. 下标 === currentIdx 的步骤，根据是否有完成后缀决定 active/done；
 *  4. 其余为 pending（尚未开始）。
 *  5. 特例：流式已结束且拿到了结果（config 或 data），则全部标记为 done。
 */
const pipelineSteps = computed(() => {
  const currentStage = store.currentStage
  const stages = activeStages.value

  // 如果没有 pipeline 定义，返回空数组
  if (!stages || stages.length === 0) {
    return []
  }

  const result = stages.map((s, i) => {
    // 每个步骤的初始状态都是 pending（待执行）
    let status: 'pending' | 'active' | 'done' = 'pending'
    if (currentStage) {
      // 用前缀匹配定位后端当前上报的步骤下标（findIndex 返回第一个匹配项的下标）
      const currentIdx = stages.findIndex(x => currentStage.startsWith(x.key))
      if (currentIdx > i) {
        status = 'done'             // 后续步骤在跑 → 本步骤已完成
      } else if (currentIdx === i) {
        // 正是当前步骤：看是否带完成后缀，带则 done，否则 active
        status = COMPLETE_SUFFIX.test(currentStage) ? 'done' : 'active'
      }
    }
    // 展开运算符：复制原对象并追加 index/status（不修改原数据）
    return { ...s, index: i + 1, status }
  })
  // 特例：流式结束且已产出结果 → 全部步骤置为 done，避免进度条卡在最后一步
  // currentConfig 用于 config 结果; _hasDataResult 用于 data 结果
  const _hasDataResult = !store.streaming && store.stageMessage === '' &&
    store.messages.length > 0 && store.messages[store.messages.length - 1]?.dataResult
  if ((!store.streaming && store.stageMessage === '' && store.currentConfig) || _hasDataResult) {
    return result.map(s => ({ ...s, status: 'done' }))
  }
  return result
})

/** 点击欢迎页示例卡片：把示例 prompt 直接作为消息发送 */
function quickFill(text: string) {
  store.sendMessage(text)
}

/** ChatInput 子组件 emit 的 send 事件处理：统一转发给 store。 */
function handleSend(text: string, imageBase64?: string) {
  // 统一入口：后端自动识别意图（create/modify/general/image）
  store.sendMessage(text, imageBase64)
}

/** 点击配置卡片：把该配置设为当前配置（侧边 JSON 面板会同步刷新） */
function selectConfig(config: FormConfig) {
  store.currentConfig = config
}

/**
 * 应用配置到主系统（仅嵌入模式可见的按钮）。
 * 通过 postMessage 把配置推给父窗口（宿主页面），由宿主写入业务表单。
 */
function applyConfig(config: FormConfig) {
  // 深拷贝为纯 JSON，避免 Vue 响应式代理对象导致 DataCloneError
  // （postMessage 内部用结构化克隆算法，无法克隆 Vue 的 Proxy 代理对象）
  const plainConfig = JSON.parse(JSON.stringify(config))
  // window.parent 是父窗口（嵌入本应用的页面）；'*' 表示不校验目标源
  window.parent.postMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config: plainConfig } }, '*')
}

// ===== 自动滚动到底部 =====
// watch：监听消息数量变化，新增消息后把列表滚到底部（类比 PropertyChangeListener）
// nextTick：等 Vue 完成 DOM 更新后再滚动，确保新消息已渲染到 DOM
watch(() => store.messages.length, () => {
  nextTick(() => {
    if (msgListRef.value) msgListRef.value.scrollTop = msgListRef.value.scrollHeight
  })
})

// 阶段提示文案变化时也滚动（pipeline 进度条更新会撑高内容）
watch(() => store.stageMessage, () => {
  nextTick(() => {
    if (msgListRef.value) msgListRef.value.scrollTop = msgListRef.value.scrollHeight
  })
})
</script>

<style scoped>
/* =============================================================================
   样式区（scoped：仅作用于本组件，Vue 自动加唯一属性选择器实现隔离）
   类比 Java：相当于组件私有的样式表，不会污染其它组件。
   ============================================================================= */

/* 主容器：纵向弹性布局，撑满父容器 */
.chat-panel { flex: 1; display: flex; flex-direction: column; min-height: 0; }

/* 消息列表：flex:1 占满剩余高度，纵向可滚动 */
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 24px 0;
}

/* ===== 欢迎屏样式 ===== */
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

/* ===== 消息气泡区域 ===== */
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

/* ===== 数据卡片样式（非配置类结果） ===== */
.data-card {
  margin-top: 10px;
  border: 1px solid var(--border-color-light);
  border-radius: var(--radius-lg);
  background: var(--bg-container);
  overflow: hidden;
  transition: all 0.2s;
}
.data-card:hover {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-md);
}
.data-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color-lighter);
  background: linear-gradient(to right, #f0f7ff, transparent);
}
.data-icon { color: var(--color-primary) !important; }
.data-card-body {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.data-field {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.data-field-label {
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 80px;
  flex-shrink: 0;
}
.data-field-value {
  font-size: 14px;
  color: var(--text-primary);
  font-weight: 500;
}
.data-card-actions {
  display: flex;
  gap: 8px;
  padding: 0 16px 12px;
}

/* ===== 配置卡片样式（表单配置结果） ===== */
.config-card {
  margin-top: 10px;
  border: 1px solid var(--border-color-light);
  border-radius: var(--radius-lg);
  background: var(--bg-container);
  overflow: hidden;
  cursor: pointer;
  transition: all 0.2s;
}

/* ===== 追问卡片样式（需要用户补充信息时） ===== */
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

/* ===== Pipeline 进度条样式（流式生成中显示） ===== */
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
