import ResultCard from './ResultCard.vue'

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const optionalText = (value: unknown): boolean => value == null || typeof value === 'string'

/** web_search 联网搜索结果卡:LLM 总结回答+[来源:N] 角标+来源卡横滚 */
function canRenderWebSearch(message: unknown): boolean {
  if (!record(message) || message.role !== 'assistant' || message.kind === 'error-diag' || message.needsClarification) return false
  const art = message.dataResult
  if (!record(art) || art.pack !== 'knowledge_graph' || art.resultDisplay !== 'card' || art.type !== 'web_search_result') return false
  if (typeof art.answer !== 'string' || !art.answer.trim()) return false
  if (!Array.isArray(art.results)) return false
  return art.results.every((r: unknown) => record(r) &&
    typeof r.url === 'string' && optionalText(r.title) && optionalText(r.content))
}

/** KG 检索结果卡:回答+子图+片段引用 */
function canRenderKgSearch(message: unknown): boolean {
  if (!record(message) || message.role !== 'assistant' || message.kind === 'error-diag' || message.needsClarification) return false
  const art = message.dataResult
  if (!record(art) || art.pack !== 'knowledge_graph' || art.resultDisplay !== 'card' || art.type !== 'kg_search_result') return false
  if (typeof art.answer !== 'string' || !art.answer.trim()) return false
  const sub = art.subgraph
  const sources = art.sources
  if (!record(sub) || !record(sources)) return false
  return Array.isArray(sub.nodes) && sub.nodes.every((node) =>
    record(node) && typeof node.id === 'string' && typeof node.name === 'string') &&
    Array.isArray(sub.edges) && sub.edges.every((edge) =>
      record(edge) && typeof edge.source === 'string' && typeof edge.target === 'string') &&
    (sources.entities == null || (Array.isArray(sources.entities) && sources.entities.every((entity) => typeof entity === 'string'))) &&
    Array.isArray(sources.chunks) && sources.chunks.every((chunk) =>
      record(chunk) && ['text', 'docName', 'docId', 'kbId', 'kbName'].every((key) => optionalText(chunk[key])))
}

export default {
  component: ResultCard,
  canRender(message: unknown): boolean {
    if (canRenderWebSearch(message)) return true
    return canRenderKgSearch(message)
  },
}
