// 链路/调用明细的共享格式化 —— 把 call_logs 的原始 request/response
// 数据翻译成带标签的人读明细(LLM 指令与输出/图谱命中/向量匹配片段),
// 聊天窗"本轮链路"弹窗、管理端"会话链路追踪"、"调用日志"抽屉三处共用。
// 按线上真实 trace 数据形状编写(见 traceDetail.test.ts 样例);未知形状
// 走通用扁平化兜底,原始 JSON 始终保留可钻取。
export interface TraceCallLike {
  callType?: string
  stage?: string | null
  requestData?: Record<string, any> | null
  responseData?: Record<string, any> | null
  [k: string]: unknown
}

export interface DetailSection {
  title?: string
  rows?: [string, string][]   // 标签-值 行
  list?: string[]             // 列表项
  text?: string               // 文本块
}

const clip = (s: unknown, n = 500): string => {
  const t = String(s ?? '').trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}
const pct = (v: unknown): string => `${Math.round(Number(v) * 100)}%`

// LLM 调用:指令摘要 + 模型 + 输出文本 + token
function fmtLlm(item: TraceCallLike): DetailSection[] {
  const req = item.requestData || {} as any
  const resp = item.responseData || {} as any
  const out: DetailSection[] = []
  const msgs: any[] = Array.isArray(req.messages) ? req.messages : []
  const lastUser = [...msgs].reverse().find(m => m && m.role === 'user')
  const rows: [string, string][] = []
  if (req.model) rows.push(['模型', String(req.model)])
  if (lastUser) rows.push(['指令摘要', clip(lastUser.content, 160)])
  if (rows.length) out.push({ rows })
  const content = clip(resp.content, 500)
  if (content) out.push({ title: '输出', text: content })
  const usage = resp.usage || {}
  const toks: [string, string][] = []
  if (usage.prompt_tokens != null) toks.push(['输入 token', String(usage.prompt_tokens)])
  if (usage.completion_tokens != null) toks.push(['输出 token', String(usage.completion_tokens)])
  if (toks.length) out.push({ rows: toks })
  return out
}

// 图谱:实体查找(kg.find_entities)
function fmtFindEntities(item: TraceCallLike): DetailSection[] {
  const req = item.requestData || {} as any
  const resp = item.responseData || {} as any
  const out: DetailSection[] = []
  const rows: [string, string][] = []
  if (Array.isArray(req.terms) && req.terms.length) rows.push(['检索词', req.terms.join('、')])
  if (resp.hits != null) rows.push(['命中', `${resp.hits} 个实体`])
  if (rows.length) out.push({ rows })
  const detail: any[] = Array.isArray(resp.termDetail) ? resp.termDetail : []
  if (detail.length) {
    out.push({ title: '命中明细', list: detail.slice(0, 10).map((t) =>
      `${t.term} → ${t.matched}（${t.type}）`) })
  }
  return out
}

// 图谱:子图扩展(kg.subgraph)
function fmtSubgraph(item: TraceCallLike): DetailSection[] {
  const req = item.requestData || {} as any
  const resp = item.responseData || {} as any
  const out: DetailSection[] = []
  const rows: [string, string][] = []
  if (Array.isArray(req.seeds) && req.seeds.length) rows.push(['种子实体', req.seeds.join('、')])
  if (resp.nodes != null) {
    rows.push(['子图', `${resp.nodes} 节点 · ${resp.edges ?? '?'} 关系`
      + ((resp.nodesTruncated || resp.edgesTruncated) ? '（已截断）' : '')])
  }
  if (rows.length) out.push({ rows })
  const triples: string[] = Array.isArray(resp.triples) ? resp.triples : []
  if (triples.length) out.push({ title: '关系', list: triples.slice(0, 10) })
  return out
}

// 向量检索(kg.vector_search)
function fmtVector(item: TraceCallLike): DetailSection[] {
  const req = item.requestData || {} as any
  const resp = item.responseData || {} as any
  const out: DetailSection[] = []
  const rows: [string, string][] = []
  if (req.query) rows.push(['检索词', clip(req.query, 120)])
  if (resp.hits != null) {
    rows.push(['命中', `${resp.hits} 段`])
    if (resp.topScore != null) rows.push(['相似度', `最高 ${pct(resp.topScore)} · 平均 ${pct(resp.meanScore)}`])
  }
  if (rows.length) out.push({ rows })
  const results: any[] = Array.isArray(resp.results) ? resp.results : []
  if (results.length) {
    out.push({ title: '匹配片段', list: results.slice(0, 5).map((r) =>
      `${pct(r.score)} · ${r.doc}：${clip(r.text, 80)}`) })
  }
  return out
}

// 通用兜底:标量入行(空值过滤),数组/对象计数量;无可展示内容返回 []
function fmtGeneric(item: TraceCallLike): DetailSection[] {
  const out: DetailSection[] = []
  for (const [name, data] of [['请求', item.requestData], ['响应', item.responseData]] as const) {
    if (!data || typeof data !== 'object') continue
    const rows = scalarRows(data as Record<string, unknown>)
    if (rows.length) out.push({ title: name, rows: rows.slice(0, 8) })
  }
  return out
}

// 标量键值对 → 行(跳过 null/空串/内部字段,数组计数量);空值不产生行
function scalarRows(data: Record<string, unknown>, maxLen = 120): [string, string][] {
  const rows: [string, string][] = []
  for (const [k, v] of Object.entries(data)) {
    if (v == null || v === '' || k === 'stage' || k === 'kbId') continue
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      const text = clip(v, maxLen)
      if (text) rows.push([k, text])
    } else if (Array.isArray(v)) {
      rows.push([k, `${v.length} 项`])
    }
  }
  return rows
}

// 向量化(kg.query_embed):输入 N 条 M 字 → 输出 N 条 D 维向量
function fmtQueryEmbed(item: TraceCallLike): DetailSection[] {
  const req = item.requestData || {} as any
  const resp = item.responseData || {} as any
  const rows: [string, string][] = []
  if (req.count != null || req.chars != null) {
    rows.push(['输入', `${req.count ?? '?'} 条 · ${req.chars ?? '?'} 字`
      + (req.backend ? ` · ${req.backend === 'local' ? '本地模型' : req.backend}` : '')])
  }
  if (resp.count != null || resp.dim != null) {
    rows.push(['输出', `${resp.count ?? '?'} 条 · ${resp.dim ?? '?'} 维向量`])
  }
  return rows.length ? [{ rows }] : []
}

// 事件类条目(无 request/response):payload.detail 有对象才展开为行
function fmtEventDetail(item: TraceCallLike): DetailSection[] {
  const detail = (item as any).payload?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return []
  const rows = scalarRows(detail as Record<string, unknown>, 160)
  return rows.length ? [{ rows }] : []
}

// 入口:按 stage/callType 分派格式化器
export function formatDetail(item: TraceCallLike): DetailSection[] {
  const stage = String(item.stage || '')
  if (!item.callType) return fmtEventDetail(item)   // 事件类:detail 有货才显示
  // stage 特判优先于 llm:query_embed 虽是 llm 型调用,但语义是向量化
  if (stage === 'kg.query_embed') return fmtQueryEmbed(item)
  if (item.callType === 'llm') return fmtLlm(item)
  if (stage === 'kg.find_entities') return fmtFindEntities(item)
  if (stage === 'kg.subgraph') return fmtSubgraph(item)
  if (stage === 'kg.vector_search') return fmtVector(item)
  return fmtGeneric(item)
}
