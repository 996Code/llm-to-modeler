type Fields = Record<string, any>

export function biPresentation(message: any, detail?: Fields | null) {
  if (message?.role !== 'assistant' || message.kind === 'error-diag' || message.needsClarification) return null
  const artifact = message.dataResult
  if (artifact?.pack !== 'chatbi') return null
  const chart = artifact.chart_option
  if (!chart || typeof chart !== 'object' || Array.isArray(chart)) return null
  const type = chart.chart_type || chart.series?.[0]?.type
  if (type === 'table') {
    if (!Array.isArray(artifact.columns) || !artifact.columns.length
      || !artifact.columns.every((column: any) => typeof column === 'string')
      || !Array.isArray(artifact.rows_sample) || !artifact.rows_sample.length
      || !artifact.rows_sample.every((row: any) => Array.isArray(row) && row.length > 0)) return null
  } else if (type === 'kpi') {
    const value = chart.series?.[0]?.data?.[0]?.value
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return null
  } else {
    if (!Array.isArray(chart.series) || !chart.series.length
      || !chart.series.every((series: any) => series && ['bar', 'line', 'pie', 'scatter'].includes(series.type)
        && Array.isArray(series.data) && series.data.length > 0)) return null
  }
  const history: Fields = {
    rowcount: artifact.rowcount,
    truncated: artifact.truncated,
    metricHits: artifact.metric_hits || [],
    persistWarnings: artifact.persist_warnings || [],
    totalDurationMs: artifact.total_duration_ms,
    executeDurationMs: artifact.execute_duration_ms,
    healRounds: artifact.heal_rounds,
    chartDegraded: artifact.chart_degraded,
    retrievalDegraded: artifact.retrieval_degraded,
  }
  // History does not persist formattedData; live tracing can add newer details.
  const live = Object.fromEntries(Object.entries(detail || {}).filter(([, value]) => value !== undefined))
  return {
    chart,
    answer: typeof artifact.answer === 'string' && artifact.answer.trim() ? artifact.answer : message.content || '',
    detail: { ...history, ...live },
  }
}
