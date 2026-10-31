// 本轮链路「简明视图」映射 —— 把引擎级打点翻译成用户能看懂的步骤。
// 开发者细节(checkpoint/请求初始化参数/意图路由/会话标题生成/token/原始
// JSON)默认隐藏;弹窗内可切「开发者」模式看全量。两类标签映射在此单一
// 维护,简明/开发者两视图共用。
export interface TraceItem {
  type: 'event' | 'call'
  kind?: string
  callType?: string
  stage?: string | null
  durationMs?: number | null
  statusCode?: number | null
  errorMessage?: string | null
  at: string
  payload?: Record<string, any>
  mergedCount?: number   // 简明视图:相邻同类调用合并后的次数
  [k: string]: unknown
}

// 阶段名 → 中文标签(开发者模式用;未知原样展示)
export const STAGE_LABELS: Record<string, string> = {
  route_pack: '意图路由', route_tool: '工具选择', compress_history: '历史压缩',
  'chatbi.embed': '问题向量化', 'chatbi.retrieve': '表结构检索',
  'chatbi.retrieve.refine': '检索精筛', 'chatbi.think': '预思考',
  'chatbi.generate_sql': 'SQL 生成', 'chatbi.execute': 'SQL 执行',
  'chatbi.check': '结果校验', 'chatbi.heal': 'SQL 自愈',
  'chatbi.chart': '图表生成', 'chatbi.memory.extract': '记忆提炼',
  'chatbi.memory.consolidate': '记忆整理',
}

// 简明模式:LLM 阶段的人话说法(优先于 STAGE_LABELS)
const AI_STAGES: Record<string, string> = {
  'assistant.select': 'AI 理解需求',
  'assistant.extract': 'AI 提取关键信息',
  'assistant.conclude': 'AI 生成结论',
  'kg.answer': 'AI 组织回答',
  'kg.intent': 'AI 分析问题',
  'kg.query': 'AI 分析问题',
  'kg.query_embed': '问题向量化',
  'kg.extract': 'AI 生成检索要点',
  'kg.summary': 'AI 生成摘要',
  'kg.rerank': 'AI 精筛资料',
  'kg.sufficiency': 'AI 评估资料',
  'chatbi.think': 'AI 预思考',
  'chatbi.generate_sql': 'AI 生成查询',
}
// 简明模式:非 LLM 调用的人话说法
const CALL_TYPES: Record<string, string> = {
  graph: '知识图谱检索', vector: '向量检索', fulltext: '全文检索',
  upstream: '查询业务系统',
}
// 简明模式隐藏的开发者细节
const HIDDEN_AI_STAGES = new Set(['conversation_title', 'compress_history', 'route_pack', 'route_tool'])
const HIDDEN_TRACE_TITLES = new Set(['checkpoint', '请求初始化参数', '意图路由'])

export function isDevOnly(item: TraceItem): boolean {
  if (item.type === 'call' && item.callType === 'llm') {
    return HIDDEN_AI_STAGES.has(item.stage || '')
  }
  if (item.type === 'event') {
    // 简明时间线只收 user/stage/带标题 trace;checkpoint 与未知事件类型都是开发者细节
    if (item.kind === 'user' || item.kind === 'stage') return false
    if (item.kind === 'trace') {
      const title = String(item.payload?.title || '')
      return !title || HIDDEN_TRACE_TITLES.has(title)
    }
    return true
  }
  return false
}

// 简明模式条目标签
export function simpleLabel(item: TraceItem): string {
  if (item.type === 'call') {
    if (item.callType === 'llm') {
      return AI_STAGES[item.stage || ''] || STAGE_LABELS[item.stage || ''] || 'AI 处理'
    }
    return CALL_TYPES[item.callType || ''] || '系统调用'
  }
  const kind = item.kind || ''
  if (kind === 'user') return '你的提问'
  if (kind === 'stage') return String(item.payload?.message || '处理中')
  if (kind === 'trace') {
    const title = String(item.payload?.title || '')
    // 标题可能带参数(如 "执行工具 metrology_assistant")——前缀命中即认
    if (title.startsWith('执行工具')) return '执行查询'
    return title || '处理节点'
  }
  return '处理节点'
}

// 简明模式副标签:执行查询带工具摘要,失败调用带错误
export function simpleSub(item: TraceItem): string {
  if (item.type === 'event' && item.kind === 'trace') {
    const detail = item.payload?.detail
    if (detail && typeof detail.summary === 'string') return detail.summary
    return ''
  }
  if (item.type === 'call' && item.errorMessage) return item.errorMessage
  return ''
}

// 简明模式时间线:过滤开发者条目 + 同名阶段消息去重(阶段消息是"阶段
// 播报",重复出现不携带新信息) + 相邻同类调用合并(两次图谱检索→×2)
export function simpleItems(items: TraceItem[]): TraceItem[] {
  const out: TraceItem[] = []
  const seenStages = new Set<string>()
  for (const it of items) {
    if (it.type === 'event' && it.kind === 'assistant') continue // 制品快照
    if (isDevOnly(it)) continue
    if (it.type === 'event' && it.kind === 'stage') {
      const msg = String(it.payload?.message || '')
      if (seenStages.has(msg)) continue
      seenStages.add(msg)
    }
    const prev = out[out.length - 1]
    // 相邻同类调用按"显示标签"合并(如 find_entities+subgraph 两次图谱
    // 检索 → 知识图谱检索 ×2):次数与耗时累加
    if (it.type === 'call' && prev && prev.type === 'call'
        && simpleLabel(prev) === simpleLabel(it)) {
      prev.durationMs = (prev.durationMs || 0) + (it.durationMs || 0)
      prev.mergedCount = (prev.mergedCount || 1) + 1
      continue
    }
    out.push({ ...it })
  }
  return out
}

// 简明模式汇总:总耗时由调用方给,这里数次数
export function simpleStats(items: TraceItem[]): { ai: number; retrieve: number; upstream: number } {
  let ai = 0, retrieve = 0, upstream = 0
  for (const it of items) {
    if (it.type !== 'call') continue
    if (it.callType === 'llm' && !isDevOnly(it)) ai++
    else if (it.callType === 'graph' || it.callType === 'vector' || it.callType === 'fulltext') retrieve++
    else if (it.callType === 'upstream' || it.callType === 'mcp') upstream++
  }
  return { ai, retrieve, upstream }
}
