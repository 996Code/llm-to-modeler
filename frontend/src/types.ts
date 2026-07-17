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
  needsClarification?: boolean
  clarificationQuestions?: string[]
  createdAt?: string
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
  valid?: boolean
  fieldCount?: number
  formName?: string
  formCode?: string
  validationErrors?: Array<{ message: string }>
  summary: string
  needsClarification?: boolean
  questions?: string[]
  intent?: string  // "create" | "modify" | "general"
}
