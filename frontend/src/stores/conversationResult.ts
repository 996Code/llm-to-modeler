import type { FormConfig, Message, SSEResult } from '../types'
import { classifyError } from '../utils/errorDiag'

export interface ConversationResultState {
  messages: Message[]
  currentConfig: FormConfig | null
  baselineConfig: FormConfig | null
}

export function applyConversationResult(result: SSEResult, state: ConversationResultState): void {
  if (result.error) {
    state.messages.push({
      role: 'assistant',
      content: `⚠️ ${result.summary || result.message || '执行失败'}`,
      kind: 'error-diag',
      errorCategory: classifyError(`${result.error || ''} ${result.summary || ''} ${result.message || ''}`),
    })
    return
  }

  if (result.intent === 'general') {
    state.messages.push({
      role: 'assistant',
      content: result.summary,
    })
    return
  }

  if (result.needsClarification && result.questions) {
    state.messages.push({
      role: 'assistant',
      content: result.summary,
      needsClarification: true,
      clarificationQuestions: result.questions,
    })
    return
  }

  if (result.artifactType === 'data') {
    const formattedData: Record<string, any> = {}
    if (result.fieldCount !== undefined) formattedData.fieldCount = result.fieldCount
    if (result.formName !== undefined) formattedData.formName = result.formName
    if (result.title !== undefined) formattedData.title = result.title
    if (result.chart !== undefined) formattedData.chart = result.chart
    if (result.metricHits !== undefined) formattedData.metricHits = result.metricHits
    if (result.rowcount !== undefined) formattedData.rowcount = result.rowcount
    if (result.datasourceName !== undefined) formattedData.datasourceName = result.datasourceName
    if (result.totalDurationMs !== undefined) formattedData.totalDurationMs = result.totalDurationMs
    if (result.healRounds !== undefined) formattedData.healRounds = result.healRounds
    if (result.executeDurationMs !== undefined) formattedData.executeDurationMs = result.executeDurationMs
    if (result.chartDegraded !== undefined) formattedData.chartDegraded = result.chartDegraded
    if (result.persistWarnings?.length) formattedData.persistWarnings = result.persistWarnings
    if (result.retrievalDegraded !== undefined) formattedData.retrievalDegraded = result.retrievalDegraded
    if (result.truncated !== undefined) formattedData.truncated = result.truncated
    if (result.tables !== undefined) formattedData.tables = result.tables
    if (result.savedQueryId !== undefined) formattedData.savedQueryId = result.savedQueryId
    if (result.sql !== undefined) formattedData.sql = result.sql
    if (result.datasourceId !== undefined) formattedData.datasourceId = result.datasourceId
    if (result.chartConfig !== undefined) formattedData.chartConfig = result.chartConfig
    state.messages.push({
      role: 'assistant',
      content: result.summary,
      dataResult: result.data || {},
      formattedData: Object.keys(formattedData).length > 0 ? formattedData : undefined,
    })
    return
  }

  if (result.config) {
    if (state.baselineConfig == null && state.currentConfig != null) {
      state.baselineConfig = JSON.parse(JSON.stringify(state.currentConfig))
    }
    state.currentConfig = result.config
    const formattedData: Record<string, any> = {}
    if (result.fieldCount !== undefined) formattedData.fieldCount = result.fieldCount
    if (result.formName !== undefined) formattedData.formName = result.formName
    if (result.formCode !== undefined) formattedData.formCode = result.formCode
    if (result.title !== undefined) formattedData.title = result.title
    if (result.valid !== undefined) formattedData.valid = result.valid
    state.messages.push({
      role: 'assistant',
      content: result.summary,
      configSnapshot: result.config,
      formattedData: Object.keys(formattedData).length > 0 ? formattedData : undefined,
    })
  }
}
