// 管理端共享标签:调用类型与环节中文名。
// 单一事实源——CallLogsTab(调用日志)与 ConversationTrace(会话链路)共用;
// 新插件的环节名加在这里(第二个带 stage 的插件出现后,可迁到各 pack 目录
// 由注册表合并,与 packPages/registry.ts 同模式)。

/** 调用类型元数据:标签 + 颜色(graph/vector 是知识图谱检索两路) */
export const CALL_TYPE_META: Record<string, { label: string; color: string }> = {
  llm: { label: 'LLM', color: 'purple' },
  upstream: { label: '上游', color: 'cyan' },
  graph: { label: '图谱', color: 'geekblue' },
  vector: { label: '向量', color: 'green' },
}

/** 调用类型在链路时间线的样式类(轨道色 + 标签底色) */
export const CALL_TYPE_CLS: Record<string, string> = {
  llm: 'tg-llm',
  upstream: 'tg-up',
  graph: 'tg-graph',
  vector: 'tg-vector',
}

/** stage → 中文环节名(管理端链路/调用日志据此区分调用环节) */
export const STAGE_LABELS: Record<string, string> = {
  route_pack: '意图路由·选领域',
  route_tool: '意图路由·选工具',
  compress_history: '历史压缩',
  'create_form.parse': '表单解析',
  'create_form.generate': '表单生成',
  'get_form.parse': '表单码解析',
  'image_form.analyze': '图片识别',
  'image_form.generate': '图片转配置',
  'clone_form.parse': '克隆解析',
  'chat.reply': '闲聊回复',
  'submit_leave.parse': '请假信息提取',
  // 知识图谱检索链路(LLM 三步 + 图/向量库两路)
  'kg.query': 'LLM·检索意图解析',
  'kg.query_embed': 'LLM·查询向量化',
  'kg.answer': 'LLM·组织回答',
  'kg.find_entities': '图谱·种子实体匹配',
  'kg.subgraph': '图谱·子图召回',
  'kg.vector_search': '向量·相似检索',
}

export function stageLabel(stage: string | null | undefined, fallback?: string): string {
  if (stage && STAGE_LABELS[stage]) return STAGE_LABELS[stage]
  return stage || fallback || '调用'
}
