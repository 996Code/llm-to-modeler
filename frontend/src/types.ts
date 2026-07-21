// API types

export interface FormConfig {
  formCode: string
  formName: string
  formColumnsNumber: number
  titleFieldKey: string
  formTitle: string
  formFieldConfigVos: FormFieldConfig[]
  topButtons?: any[]
  bottomButtons?: any[]
  [key: string]: any
}

export interface FormFieldConfig {
  formFieldType: number
  fieldTitleKey: string
  fieldTitleText: string
  fieldWidth: number
  [key: string]: any
}

export interface Message {
  id?: string
  role: 'user' | 'assistant'
  content: string
  configSnapshot?: FormConfig | null
  formattedData?: Record<string, any>  // SSE result 透传的格式化字段(如 fieldCount, formName 等)
  dataResult?: Record<string, any>     // artifactType='data' 时的结构化数据
  needsClarification?: boolean
  clarificationQuestions?: ClarificationQuestion[]  // 后端 AskQuestion 对象(非纯字符串)
  createdAt?: string
}

/** 追问问题 — 对应后端 AskQuestion model */
export interface ClarificationQuestion {
  question: string
  header: string
  options: ClarificationOption[]
  multi_select?: boolean
}

/** 追问选项 — 对应后端 AskOption model */
export interface ClarificationOption {
  label: string
  description: string
}

export interface Conversation {
  id: string
  title: string
  currentConfig?: FormConfig | null
  messages?: Message[]
  createdAt?: string
  updatedAt?: string
}

export interface SSEResult {
  config?: FormConfig
  artifactType?: 'config' | 'data'  // 区分配置结果和数据结果
  data?: Record<string, any>         // artifactType='data' 时的数据
  valid?: boolean
  fieldCount?: number
  formName?: string
  formCode?: string
  title?: string
  validationErrors?: Array<{ message: string }>
  summary: string
  needsClarification?: boolean
  questions?: ClarificationQuestion[]  // 后端 AskQuestion 对象(非纯字符串)
  intent?: string  // "create" | "modify" | "general"
}
