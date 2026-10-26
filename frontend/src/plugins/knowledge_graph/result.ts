import ResultCard from './ResultCard.vue'

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const optionalText = (value: unknown) => value == null || typeof value === 'string'

export default {
  component: ResultCard,
  canRender(message: unknown): boolean {
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
  },
}
