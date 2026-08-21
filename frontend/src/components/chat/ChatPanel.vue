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
          <!-- 图标名来自 pack manifest（display.welcome_icon），未知/缺省回退表单无关的默认 -->
          <component :is="welcomeIcon" />
        </div>
        <h1 class="welcome-title">智能助手</h1>
        <p class="welcome-subtitle">用自然语言描述你的需求，我来帮你完成</p>
        <!-- v-if 守卫：无示例数据（manifest 未就绪/未声明）时不渲染空容器 -->
        <div v-if="examples.length" class="examples">
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
                <!-- @click.stop：阻止事件冒泡（避免触发外层 config-card 的点击）。
                     按钮按 pack 声明的动作集渲染（actions: view_json/apply/rewind），
                     组件不硬编码——下个插件的制品卡可声明不同的动作组合 -->
                <a-button
                  v-if="packActions.includes('view_json')"
                  size="small" type="link"
                  @click.stop="showJsonViewer(msg.configSnapshot)"
                >
                  <EyeOutlined /> 查看 JSON
                </a-button>
                <!-- 切换到此版本（git checkout 式：对话保留，只切制品）：
                     pack 声明 rewind 且该版不是当前生效版本时显示（包括最后
                     一张卡——从旧版跳回最新同样走这里） -->
                <a-button
                  v-if="packActions.includes('rewind') && !isCurrentVersion(msg)"
                  size="small"
                  type="link"
                  :loading="rewinding"
                  @click.stop="rewindToVersion(msg)"
                >
                  <RollbackOutlined /> 回滚到此版本
                </a-button>
                <!-- 仅嵌入模式显示「应用」：pack 声明 apply 且宿主 capabilities
                     支持（双重判定），走 HostPort 协议渲染到宿主 -->
                <a-button
                  v-if="packActions.includes('apply') && embedded && port.capabilities.has('apply')"
                  size="small"
                  type="primary"
                  :loading="applying"
                  @click.stop="applyConfig(msg.configSnapshot)"
                >
                  <CheckOutlined /> 应用
                </a-button>
                <!-- 宿主不支持 apply：退化为复制 JSON。注意此项独立于 packActions
                     声明——复制是任何制品的最后出口（逃生口原则），即便 pack
                     未声明 view_json 也保留内容带走的能力 -->
                <a-button v-if="!port.capabilities.has('apply')" size="small" type="link" @click.stop="copyConfig(msg.configSnapshot)">
                  <CopyOutlined /> 复制 JSON
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

    <!-- JSON 查看器 Modal：全屏（嵌入模式下 iframe 只有 420px 宽，弹窗必须拉满
         视口才有可读性；撑大悬浮窗本体由 RESIZE 协议承担，见 showJsonViewer） -->
    <Modal
      v-model:open="jsonViewerVisible"
      title="配置 JSON"
      :footer="null"
      width="100%"
      wrapClassName="json-viewer-fullscreen"
      :bodyStyle="{ padding: 0, flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }"
    >
      <!-- 变更视图：红删绿增（基线=画布/上一版），无基线时显示完整 JSON -->
      <div class="json-viewer-body">
        <JsonDiffView :oldObj="store.baselineConfig" :newObj="jsonViewerData" />
      </div>
    </Modal>
  </div>
</template>

<script setup lang="ts">
// ===== Vue Composition API 引入（类比 Java 的 import） =====
// ref/computed/watch：见文件顶部说明；nextTick：等下一次 DOM 更新后再执行（用于滚动到底部）
import { ref, computed, watch, nextTick, onMounted } from 'vue'
// 图标组件（ant-design-vue 图标库，按需引入以减小打包体积）
import {
  UserOutlined, FormOutlined, TableOutlined, CheckOutlined,
  CheckCircleOutlined, EyeOutlined, QuestionCircleOutlined,
  CopyOutlined, RollbackOutlined,
  SolutionOutlined, TeamOutlined, ContactsOutlined,
} from '@ant-design/icons-vue'
// Modal：ant-design-vue 的弹窗组件（模板里的 JSON 查看器）
import { Modal, message as antdMessage } from 'ant-design-vue'
// 全局会话 Store（Pinia 单例），类比 @Autowired private ConversationStore store
import { useConversationStore } from '../../stores/conversation'
// 仅引入类型（编译期检查，运行时不打包）
import type { FormConfig } from '../../types'
// 子组件：输入框
import ChatInput from './ChatInput.vue'
// 子组件：JSON 变更视图（查看弹窗用，红删绿增）
import JsonDiffView from '../json/JsonDiffView.vue'
// HostPort 单例：UI 只依赖 hostPort 抽象，不直接碰 postMessage
import { getHostPort } from '../../composables/hostPort'
// 通用 diff（快照摘要的变更数统计）
import { diffJson } from '../../utils/diff'
import { getPackManifests } from '../../services/api'

// ===== 组件入参（props）声明 =====
// defineProps<{ embedded?: boolean }>() —— 编译宏，声明本组件接收一个可选的 embedded 标记。
// 【类比 Java】相当于接口契约：父组件传递 embedded=true 表示当前是嵌入模式。
// 嵌入模式下配置卡片会显示「应用」按钮（走 HostPort 协议发给宿主）。
defineProps<{ embedded?: boolean }>()

// 获取全局 store 实例（setup 中调用一次，整个组件生命周期复用）
const store = useConversationStore()
// HostPort 单例：能力决定按钮是否可用（宿主无 apply → 自动退化复制 JSON）
const port = getHostPort()

// pack 的展示声明（data_labels/data_hidden/welcome_examples）——领域词住 manifest，
// 本组件只按通用字段渲染。拉取失败回退空（不显示示例卡、key 不翻译）
const packDisplay = ref<Record<string, any> | null>(null)
const packArtifactType = ref<string | undefined>(undefined)
// 制品卡动作集（pack 声明，未声明回退最小集 view_json）——不同插件的制品
// 交互不同，按钮由声明驱动而非组件硬编码
const packActions = ref<string[]>(['view_json'])
onMounted(async () => {
  try {
    const manifests = await getPackManifests()
    // 不能取 manifests[0]：pack 顺序是文件系统发现序（多 pack 时不稳定，
    // 曾把无 artifact 声明的示例 pack 排前——标题回退"智能助手"、apply
    // 按钮消失）。优先选声明了 artifact 的主交互 pack，全都没有才回退第一个
    const primary = manifests.find((m: any) => m.artifact?.display) || manifests[0]
    packDisplay.value = primary?.artifact?.display || null
    packArtifactType.value = primary?.artifact?.type
    const acts = primary?.artifact?.actions
    if (Array.isArray(acts) && acts.length) packActions.value = acts
  } catch {
    packDisplay.value = null
  }
})
// 消息列表容器的 DOM 引用，用于手动控制 scrollTop（自动滚到底部）
const msgListRef = ref<HTMLElement>()

// 应用进行中标记（防止重复点击）
const applying = ref(false)

// 回滚进行中标记（防止重复点击）
const rewinding = ref(false)

// JSON 查看器状态：弹窗显隐 + 弹窗内显示的 JSON 文本
const jsonViewerVisible = ref(false)
const jsonViewerData = ref<Record<string, any> | null>(null)

/**
 * 打开 JSON 查看器弹窗：把任意对象格式化成 2 空格缩进的 JSON 字符串后展示。
 * @param data 要展示的对象（表单配置或数据结果）
 */
function showJsonViewer(data: FormConfig | Record<string, any>) {
  // 存对象：变更视图在 JsonDiffView 内做序列化 + 行级 diff
  jsonViewerData.value = data as Record<string, any>
  jsonViewerVisible.value = true
  // 嵌入模式：请求宿主把悬浮窗临时撑大（420px 里看 diff 不可读）。
  // 宿主不支持 RESIZE 则静默无效果（弹窗仍全屏占满 iframe 视口）
  if (port.connected) port.notifyResize('expanded')
}

// 关闭查看器：恢复宿主悬浮窗原始尺寸（v-model:open 关闭路径统一收口在这里）
watch(jsonViewerVisible, (open) => {
  if (!open && port.connected) port.notifyResize('normal')
})

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
  // 标签映射与隐藏字段清单来自 pack manifest（display.data_labels / data_hidden）
  const labels: Record<string, string> = packDisplay.value?.data_labels || {}
  const hidden = new Set<string>(packDisplay.value?.data_hidden || [])
  const result: Record<string, string> = {}
  let count = 0
  for (const [key, value] of Object.entries(data)) {
    if (count >= 6) break                        // 最多展示 6 个字段
    if (hidden.has(key)) continue                // 跳过 pack 声明的内部字段
    if (typeof value === 'object' && value !== null) continue  // 跳过嵌套对象
    if (value === '' || value === null || value === undefined) continue
    const label = labels[key] || key             // pack 中文标签，缺省保留原 key
    result[label] = String(value)
    count++
  }
  return result
}

// 欢迎页示例卡：内容来自 pack manifest（display.welcome_examples），
// 本组件只提供通用图标集（名字→组件）；manifest 未提供则不显示示例区
const ICON_SET: Record<string, any> = { form: FormOutlined, contacts: ContactsOutlined }
const examples = computed(() =>
  ((packDisplay.value?.welcome_examples || []) as any[])
    .map((ex: any) => ({ ...ex, icon: ICON_SET[ex.icon] || FormOutlined }))
)

// 欢迎页大图标：manifest 的 welcome_icon 名字 → 通用图标集组件（缺省回退默认）
const welcomeIcon = computed(() => {
  const name = packDisplay.value?.welcome_icon
  return (name && ICON_SET[name]) || FormOutlined
})

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
 * 应用配置到宿主（仅嵌入模式 + 宿主支持 apply 时显示的按钮）。
 * 流程：拉宿主最新基线（漂移检测 revision）→ APPLY → 回执。
 * 变更内容已在 JSON 面板的变更视图（红删绿增）里可视化，此处不再弹摘要确认；
 * 画布即最终预览，应用成功后追加快照（版本历史可还原）并同步 diff 基线。
 * @param config 配置结果
 */
/** 该消息的制品是否就是当前生效版本（内容级比较；卡片少，全量 stringify 可接受） */
function isCurrentVersion(msg: any): boolean {
  if (!msg?.configSnapshot || !store.currentConfig) return false
  return JSON.stringify(msg.configSnapshot) === JSON.stringify(store.currentConfig)
}

/**
 * 切换到此版本（git checkout 式：**对话保留**，只切制品）。
 * 把该版本的制品设为当前（diff 基线=切换前版本，变更视图可见差异）；嵌入
 * 模式（宿主支持 apply）自动渲染回画布。之后想回到更新版本——点那张卡的
 * 本按钮即可（对话里所有版本卡互为跳转锚点，包括"反悔用最新"）。
 */
function rewindToVersion(msg: any) {
  if (rewinding.value) return
  const canApply = port.connected && port.capabilities.has('apply')
  Modal.confirm({
    title: '切换到此版本',
    content: '当前制品将恢复为该版本' + (canApply ? '并应用到画布' : '') +
      '。对话记录保留，随时可再切换到其他版本（包括最新）。',
    okText: '切换',
    cancelText: '取消',
    onOk: async () => {
      rewinding.value = true
      try {
        // 深拷贝断引用（该版本对象与消息共享，避免后续修改互染）
        const snap = JSON.parse(JSON.stringify(msg.configSnapshot))
        const previous = store.currentConfig
        store.currentConfig = snap
        // diff 基线 = 切换前版本：变更视图显示"这次切换改了什么"；
        // 嵌入自动应用成功后 applyConfig 会把基线同步为应用版（画布=该版本）
        if (previous != null) {
          store.setBaseline(previous)
        }
        if (canApply) {
          await applyConfig(snap)
        } else {
          antdMessage.success('已切换到此版本')
        }
      } finally {
        rewinding.value = false
      }
    },
  })
}

async function applyConfig(config: FormConfig) {
  if (applying.value) return
  applying.value = true
  try {
    // 深拷贝为纯 JSON，避免 Vue 响应式代理对象导致 DataCloneError
    // （postMessage 内部用结构化克隆算法，无法克隆 Vue 的 Proxy 代理对象）
    const plainConfig = JSON.parse(JSON.stringify(config))
    // 应用前向宿主拉最新上下文：拿 revision 供宿主漂移检测（画布被手动改则 REVISION_CONFLICT）
    const hostCtx = await port.getContext()
    const summary = store.currentConfigName || 'AI 生成配置'
    const result = await port.applyArtifact({
      artifact: plainConfig,
      baseRevision: hostCtx?.revision ?? null,
      summary,
      artifactType: packArtifactType.value,
    })
    if (result.ok) {
      // 应用成功：画布与 AI 产出一致，diff 基线同步为应用版（变更视图归零）。
      store.setBaseline(result.artifact ?? plainConfig)
      antdMessage.success('已应用到画布，请确认后手动保存')
    } else {
      // 失败：按错误码给出差异化提示（REVISION_CONFLICT 提示基于最新重改）
      const hint =
        result.code === 'REVISION_CONFLICT'
          ? '画布已被手动修改，请基于最新配置重新生成后再应用'
          : result.message || '应用失败'
      antdMessage.error(hint)
    }
  } finally {
    applying.value = false
  }
}


/** 复制配置 JSON 到剪贴板（独立模式 / 宿主不支持 apply 时显示） */
async function copyConfig(config: FormConfig) {
  try {
    await navigator.clipboard.writeText(JSON.stringify(config, null, 2))
    antdMessage.success('已复制配置 JSON')
  } catch {
    antdMessage.error('复制失败，请使用「查看 JSON」手动复制')
  }
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

<!-- 全局样式块：wrapClassName 指定的 Modal 渲染在 body 直下，scoped 选择器够不着 -->
<style>
.json-viewer-fullscreen .ant-modal {
  max-width: 100%;
  margin: 0;
  padding: 0;
  top: 0;
}
.json-viewer-fullscreen .ant-modal-content {
  height: 100vh;
  display: flex;
  flex-direction: column;
  border-radius: 0;
}
.json-viewer-fullscreen .ant-modal-header {
  flex-shrink: 0;
}
.json-viewer-fullscreen .ant-modal-body {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>

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
/* 欢迎页大 Logo（渐变圆角方块） */
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
/* 欢迎页主标题 */
.welcome-title {
  font-size: 28px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 10px;
}
/* 欢迎页副标题 */
.welcome-subtitle {
  font-size: 15px;
  color: var(--text-secondary);
  margin-bottom: 40px;
}
/* 示例卡片网格：两列等宽布局 */
.examples {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  text-align: left;
}
/* 单个示例卡片：横向排列图标+文字 */
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

/* JSON 查看器内容区：撑满全屏 Modal 的 body（内部 diff 自行滚动） */
.json-viewer-body {
  flex: 1;
  overflow: auto;
  min-height: 0;
  border-top: 1px solid var(--border-color-light);
}
</style>
