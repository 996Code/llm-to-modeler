/**
 * P2#15 失败诊断:错误文本 → 失败环节分类。
 *
 * 抽为纯函数(自 store 内联抽出):供 conversation store(实时错误)与
 * 单测共用;后端 stream.py 落库侧有同口径拷贝(改关键词两处同步)。
 */
export type ErrorCategory = 'llm-upstream' | 'retrieval' | 'execution' | 'unknown'

export function classifyError(err: string): ErrorCategory {
  const t = (err || '').toLowerCase()
  // 存储设施关键词优先于通用连接词:neo4j/pgvector 的 connection 错误
  // 是检索设施故障,不是 LLM 上游(审查抓出的误分类;Milvus 已下线,
  // 关键词与后端 engine/stream.py 落库侧同口径)
  if (t.includes('neo4j') || t.includes('vector')
      || t.includes('graph') || t.includes('检索')) {
    return 'retrieval'
  }
  if (t.includes('llm') || t.includes('chat') || t.includes('completion')
      || t.includes('500') || t.includes('502') || t.includes('timeout')
      || t.includes('connection') || t.includes('embed')) {
    return 'llm-upstream'
  }
  if (t.includes('sql') || t.includes('数据库') || t.includes('执行')) {
    return 'execution'
  }
  return 'unknown'
}

/** 分类 → 用户可读文案(非技术用户视角,避免堆术语) */
export function errorCategoryLabel(cat?: string): string {
  switch (cat) {
    case 'llm-upstream': return 'AI 服务暂时不可用'
    case 'retrieval': return '知识检索遇到问题'
    case 'execution': return '数据查询失败'
    default: return '请求处理失败'
  }
}
