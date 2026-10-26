export interface MetrologyView {
  kind: 'knowledge' | 'progress' | 'notice' | 'instrument'
  title: string
  answer?: string
  decision?: string
  status?: string
  fields?: { label: string; value: string }[]
  records?: { label: string; value: string }[][]
}

const INSTRUMENT_KEYS = ['name', 'model', 'measureRange', 'accuracy', 'feeYuan',
  'mandatoryL1', 'mandatoryL2', 'periodMonths', 'regulation']

function instrumentFields(record: any, labels: Record<string, string>) {
  return INSTRUMENT_KEYS.map((key) => {
    const value = record?.[key]
    if (value === null || value === undefined || value === '') return null
    const text = key === 'periodMonths' ? `${value} 个月`
      : key === 'feeYuan' ? `${value} 元` : String(value)
    return { label: labels[key] || key, value: text }
  }).filter((f): f is { label: string; value: string } => f !== null)
}

export function metrologyPresentation(msg: any, labels: Record<string, string> = {}): MetrologyView | null {
  if (msg?.role !== 'assistant' || msg.kind === 'error-diag' || msg.needsClarification) return null
  const art = msg.dataResult
  if (!art) return null
  if (art.type === 'metrology_progress' && art.orderNo && art.status) {
    const keys = ['orderNo', 'instrument', 'status', 'progress', 'currentStep', 'eta', 'reportNo', 'finishedAt', 'validUntil', 'queriedAt']
    return { kind: 'progress', title: '检测进度', status: art.status,
      fields: keys.filter(key => art[key] !== undefined && art[key] !== null && art[key] !== '')
        .map(key => ({ label: labels[key] || key, value: String(art[key]) })) }
  }
  if (art.type === 'metrology_notice' && typeof art.message === 'string' && art.message.trim()) {
    return { kind: 'notice', title: art.title || '业务提示', status: art.status, answer: art.message }
  }
  if (art.type === 'metrology_instrument' && Array.isArray(art.instruments)) {
    // 同名多行(不同量程不同价)逐条成卡,不丢行;无可用字段则回落文字摘要
    const records = art.instruments
      .map((record: any) => instrumentFields(record, labels))
      .filter((fields: { label: string; value: string }[]) => fields.length)
    return records.length ? { kind: 'instrument', title: '器具价目信息', records } : null
  }
  if (art.type !== 'kg_search_result' || (art.pack !== 'metrology_assistant' && art.scenario !== '强检判定')) return null
  const answer = art.answer
  if (answer && typeof answer === 'object' && typeof answer.conclusion === 'string' && Array.isArray(answer.evidence)) {
    const evidence = answer.evidence.map((item: any) => {
      const refs = Array.isArray(item.chunk_refs) ? item.chunk_refs.filter((n: any) => Number.isInteger(n) && n > 0 && n <= (art.sources?.chunks?.length || 0)) : []
      return `${item.text || ''} ${refs.map((n: number) => `[片段:${n}]`).join(' ')}`.trim()
    }).filter(Boolean)
    return { kind: 'knowledge', title: '强检判定', decision: answer.decision,
      answer: [answer.conclusion, evidence.length ? `**依据**\n\n${evidence.map((text: string) => `- ${text}`).join('\n')}` : (answer.evidence_gap ? `**依据缺口**\n\n${answer.evidence_gap}` : ''),
        answer.suggestion ? `**建议**\n\n${answer.suggestion}` : ''].filter(Boolean).join('\n\n') }
  }
  const legacy = typeof answer === 'string' && answer.trim() ? answer : msg.content
  return typeof legacy === 'string' && legacy.trim() ? { kind: 'knowledge', title: '强检判定', answer: legacy } : null
}
