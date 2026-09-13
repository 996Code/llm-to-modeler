"""
SchemaGraph 包入口 —— chat-bi 知识图谱栈的统一门面。

【模块定位】
  chatbi 插件知识图谱栈的唯一导入面: 调用方 (SQL 生成链路 / 图谱 API /
  后台演化任务) 统一从本模块导入 SchemaGraph 与全部图谱函数, 不直接
  依赖内部拆分件——内部文件重排不破坏调用方 (门面模式, 与 models.py
  作为结构契约的定位配套)。

【设计要点】
  - 拆分件分工: graph_core (图谱服务核心) / graph_infer (LLM 推断与
    confidence 演化) / graph_context (表扩展门面 + JOIN 路径 prompt 块)
  - 配置注入: 全部 GRAPH_* 常量为 chat-bi config.py 同名字段的默认值,
    调用方从插件 settings (pack settings) 传入构造参数/函数参数
  - LLM 显式注入: 推断函数接收 llm 参数 (对齐 src/llm/client.py 的
    LLMClient 鸭子类型), stage 命名 chatbi.graph.* (call_logs 链路追踪)
  - 同步语义: 全栈无 async (调用方为引擎同步线程池)

【移植来源】
  chat-bi (只读源仓库) 三个文件忠实移植, 行号为源文件位置:
    - graph_core.py   ← backend/app/services/graph_service.py 全文 (753 行)
    - graph_infer.py  ← backend/app/services/knowledge_graph.py 全文 (822 行)
                        + backend/app/core/llm_json.py parse_json_response (L42-114)
    - graph_context.py ← backend/app/ai/schema_utils.py 图谱侧函数
                        (get_schema_graph L76-95 / expand_with_relationships
                        L98-175 / _expand_with_bfs L178-241 /
                        build_join_path_section L363-460)
  适配项 (移植契约允许, 无功能删减):
    1. async → sync (调用方是同步引擎线程池)
    2. LLM 全局单例 llm_chat → 显式 llm 参数; node= → stage="chatbi.graph.*"
    3. get_settings() → GRAPH_* 默认值常量 + 构造/函数参数注入
    4. 多租户维度删除; SQL 审计 / token 上报删除 (引擎 call_logs 自动记)
    5. apply_confidence_updates 的 DB 访问: AsyncSession+SQLAlchemy →
       平台 services.db 同步引擎直写 chatbi_semantic_models (models.py DDL);
       向量索引重建 → rebuild_index 回调注入 (未注入时按源语义降级不阻塞)
"""
from __future__ import annotations

# 图谱核心 (chat-bi graph_service.py)
from domains.chatbi.graph_core import (
    GRAPH_COMMUNITY_ALGORITHM,
    GRAPH_EXPAND_MAX_TOTAL,
    GRAPH_EXPAND_USE_COMMUNITY,
    GRAPH_FEEDBACK_DISCOVER_NEW_PAIRS,
    GRAPH_JOIN_PATH_IN_PROMPT,
    GRAPH_LINKAGE_CONFIDENCE_BOOST,
    GRAPH_LINKAGE_CO_OCCURRENCE_THRESHOLD,
    GRAPH_LINKAGE_NEW_PAIR_THRESHOLD,
    GRAPH_MAX_JOIN_PATH_HOPS,
    JoinPath,
    SchemaGraph,
)

# 图谱上下文 (chat-bi schema_utils.py 图谱侧)
from domains.chatbi.graph_context import (
    _expand_with_bfs,
    build_join_path_section,
    expand_with_relationships,
    get_schema_graph,
)

# 知识图谱推断与演化 (chat-bi knowledge_graph.py)
from domains.chatbi.graph_infer import (
    AI_INFERRED_CONFIDENCE,
    CORRECTION_PENALTY,
    FREQUENT_JOIN_BOOST,
    FREQUENT_JOIN_THRESHOLD,
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
    NAME_PATTERN_CONFIDENCE,
    PRAISE_BOOST,
    VersionConflictError,
    apply_confidence_updates,
    apply_feedback_signals,
    infer_knowledge_graph,
    linkage_memories_to_cooccurrence,
    mine_implicit_relationships,
    sync_linkage_to_graph,
)

__all__ = [
    # 配置常量 (chat-bi config.py 同名字段默认值)
    "GRAPH_COMMUNITY_ALGORITHM",
    "GRAPH_EXPAND_MAX_TOTAL",
    "GRAPH_EXPAND_USE_COMMUNITY",
    "GRAPH_FEEDBACK_DISCOVER_NEW_PAIRS",
    "GRAPH_JOIN_PATH_IN_PROMPT",
    "GRAPH_LINKAGE_CONFIDENCE_BOOST",
    "GRAPH_LINKAGE_CO_OCCURRENCE_THRESHOLD",
    "GRAPH_LINKAGE_NEW_PAIR_THRESHOLD",
    "GRAPH_MAX_JOIN_PATH_HOPS",
    # 图谱核心
    "JoinPath",
    "SchemaGraph",
    # 图谱上下文
    "build_join_path_section",
    "expand_with_relationships",
    "get_schema_graph",
    # 推断与演化
    "AI_INFERRED_CONFIDENCE",
    "CORRECTION_PENALTY",
    "FREQUENT_JOIN_BOOST",
    "FREQUENT_JOIN_THRESHOLD",
    "MAX_CONFIDENCE",
    "MIN_CONFIDENCE",
    "NAME_PATTERN_CONFIDENCE",
    "PRAISE_BOOST",
    "VersionConflictError",
    "apply_confidence_updates",
    "apply_feedback_signals",
    "infer_knowledge_graph",
    "linkage_memories_to_cooccurrence",
    "mine_implicit_relationships",
    "sync_linkage_to_graph",
]
