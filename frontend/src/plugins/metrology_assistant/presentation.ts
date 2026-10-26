export interface MetrologyView {
  kind: 'knowledge' | 'progress' | 'notice'
  title: string
  answer?: string
  decision?: string
  status?: string
  fields?: { label: string; value: string }[]
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
