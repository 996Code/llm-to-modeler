// =============================================================================
// 模块说明：TypeScript 类型定义（DTO / 领域模型层）
// -----------------------------------------------------------------------------
// 类比 Java：这个文件相当于一组 POJO / Record / DTO 类，纯数据结构定义，无逻辑。
//
// TS 关键字与 Java 对照：
//   interface  —— 和 Java interface 一样是"契约"，但 TS 的 interface 只描述数据形状，
//                 不含实现（更接近 Java 的 record / DTO）。
//   type       —— 类型别名，类似 Java 的 typedef（Java 无此概念，但可理解为给类型起别名）。
//   ?  可选字段 —— 类似 Java 中该字段为 Optional<T> 或可空（@Nullable），可填可不填。
//   [key: string]: any —— 索引签名，类似 Java Map<String, Object> 的开放扩展点，
//                 表示"还允许任意其它字符串键"。
// =============================================================================

// ---------- 表单配置相关 ----------

/**
 * 表单配置（对应后端的 FormConfig 模型）。
 * 这是 AI 生成的最终产物：一份完整的动态表单定义。
 */
export interface FormConfig {
  formCode: string                  // 表单编码（唯一标识，类似数据库表名）
  formName: string                  // 表单名称（中文名，展示用）
  formColumnsNumber: number         // 表单列数（布局：1 列 / 2 列 / ...）
  titleFieldKey: string             // 标题字段 key（用哪个字段作为卡片标题）
  formTitle: string                 // 表单标题文本
  formFieldConfigVos: FormFieldConfig[]  // 字段配置列表（"Vos" 后缀源自后端 VO 概念）
  topButtons?: any[]                // 顶部按钮（可选；any 表示类型不固定）
  bottomButtons?: any[]             // 底部按钮（可选）
  [key: string]: any                // 索引签名：允许后端额外扩展字段而不报类型错误
}

/**
 * 表单字段配置（对应后端 FormFieldConfig / 字段定义）。
 */
export interface FormFieldConfig {
  formFieldType: number             // 字段类型编码（数字枚举：文本/数字/日期/下拉...）
  fieldTitleKey: string             // 字段 key（类似数据库列名）
  fieldTitleText: string            // 字段显示名（中文标签）
  fieldWidth: number                // 字段宽度（占多少列）
  [key: string]: any                // 索引签名：允许额外扩展属性
}

// ---------- 对话消息相关 ----------

/**
 * 聊天消息（一条 user 或 assistant 的发言）。
 * 类比 Java：相当于聊天记录实体类（ChatMessage entity）。
 */
export interface Message {
  id?: string                       // 消息 ID（可选，后端可能不返回）
  role: 'user' | 'assistant'        // 角色：联合类型（只能是这两个字符串字面量之一）
  content: string                   // 消息正文（纯文本）
  configSnapshot?: FormConfig | null  // 该消息携带的表单配置快照（仅当结果为配置时存在）
  formattedData?: Record<string, any>  // SSE result 透传的格式化字段(如 fieldCount, formName 等)
                                     // Record<string, any> ≈ Map<String, Object>
  dataResult?: Record<string, any>     // artifactType='data' 时的结构化数据（非配置类结果）
  needsClarification?: boolean         // 是否需要用户追问澄清（后端无法直接生成时置 true）
  clarificationQuestions?: ClarificationQuestion[]  // 后端 AskQuestion 对象(非纯字符串)
  createdAt?: string                   // 创建时间（字符串形式的 ISO 时间）
}

/** 追问问题 —— 对应后端 AskQuestion model（一个问题包含标题 + 多个选项） */
export interface ClarificationQuestion {
  question: string                  // 问题文本
  header: string                    // 问题分类标题
  options: ClarificationOption[]    // 候选选项列表
  multi_select?: boolean            // 是否允许多选
}

/** 追问选项 —— 对应后端 AskOption model */
export interface ClarificationOption {
  label: string                     // 选项标签
  description: string               // 选项描述
}

/**
 * 对话（一次完整的聊天会话，包含多条消息）。
 * 类比 Java：相当于会话实体（Conversation entity），聚合了消息列表。
 */
export interface Conversation {
  id: string                        // 会话 ID
  title: string                     // 会话标题
  displayTitle?: string             // 展示标题(真实 title > 首条用户消息截断 > 新对话)
  messageCount?: number             // 消息数(列表接口子查询)
  currentConfig?: FormConfig | null // 当前会话最新配置（可选）
  messages?: Message[]              // 该会话的消息列表（可选，列表接口可能不返回）
  createdAt?: string                // 创建时间
  updatedAt?: string                // 更新时间
}

/**
 * SSE（Server-Sent Events）单次返回的结果。
 * 后端通过流式接口逐个推送 SSEResult，前端据此更新 UI。
 * 字段大多是可选的，因为不同事件类型会带不同的字段子集。
 */
export interface SSEResult {
  error?: boolean                   // 执行失败/被前置校验拦截(error_for_llm 通道)
  message?: string                  // 失败时的详细原因(与 error 搭配)
  config?: FormConfig               // 生成的表单配置（仅配置类结果有）
  artifactType?: 'config' | 'data'  // 区分配置结果和数据结果（判别式字段）
  data?: Record<string, any>         // artifactType='data' 时的数据
  valid?: boolean                    // 配置是否通过校验
  fieldCount?: number                // 字段数量（展示统计用）
  formName?: string                  // 表单名（展示用）
  formCode?: string                  // 表单编码
  title?: string                     // 结果标题
  validationErrors?: Array<{ message: string }>  // 校验错误列表
  summary: string                    // 本次结果的文字摘要（展示给用户）
  needsClarification?: boolean        // 是否需要追问
  questions?: ClarificationQuestion[]  // 后端 AskQuestion 对象(非纯字符串)
  intent?: string                    // 意图分类："create" | "modify" | "general"
  conversationId?: string            // 会话 ID(懒创建场景:首条消息不带 id,后端建好后随 result 回传)
}
