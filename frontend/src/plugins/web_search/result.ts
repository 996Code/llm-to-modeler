import WebSearchResultCard from './WebSearchResultCard.vue'

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const optionalText = (value: unknown): boolean => value == null || typeof value === 'string'

/** web_search 联网搜索结果卡:LLM 总结回答+[来源:N] 角标+来源列表。
 *  pack 限定 web_search(客服直连产出的同类型卡由 metrology 目录注册渲染)。 */
export default {
  component: WebSearchResultCard,
  canRender(message: unknown): boolean {
    if (!record(message) || message.role !== 'assistant' || message.kind === 'error-diag' || message.needsClarification) return false
    const art = message.dataResult
    if (!record(art) || art.pack !== 'web_search' || art.resultDisplay !== 'card' || art.type !== 'web_search_result') return false
    if (typeof art.answer !== 'string' || !art.answer.trim()) return false
    if (!Array.isArray(art.results)) return false
    return art.results.every((r: unknown) => record(r) &&
      typeof r.url === 'string' && optionalText(r.title) && optionalText(r.content))
  },
}
