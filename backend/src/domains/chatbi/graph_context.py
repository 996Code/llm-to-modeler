"""
图谱上下文构建 —— 表扩展门面 + JOIN 路径预计算文本（SQL prompt 注入块）。

【模块定位】
  SchemaGraph 的调用侧封装: 检索命中表 → 表扩展 (expand_with_relationships)
  → JOIN 路径预计算文本 (build_join_path_section) → 注入 SQL 生成 prompt。
  调用方统一从包入口 domains.chatbi.schema_graph 导入本模块符号。

【设计要点】
  - 用语义层结构化关系数据 (Relationship), 不硬编码命名规则;
    关系由扫描 (外键) + knowledge_graph (LLM 推断) 产出, 这里只消费不推断
  - SchemaGraph 优先, BFS 降级 (fail-open, 保证查询可用)
  - JOIN 路径预计算: LLM 自行推断 JOIN 容易出错 (选错列/类型),
    把确定性的工作放在系统侧 (对标 AEE-001: 减少 LLM 推理负担)
  - 防扩散: 种子表永远保留不受上限影响; SchemaGraph 按图距离排序近的优先
  - join_path_in_prompt 开关可关闭 JOIN 路径块 (让 LLM 自行推断)

【移植来源】
  chat-bi backend/app/ai/schema_utils.py (图谱侧四个函数) 忠实移植:
    - get_schema_graph (请求级工厂)      ← L76-95
    - expand_with_relationships          ← L98-175
    - _expand_with_bfs (降级备用)        ← L178-241
    - build_join_path_section (LEFT JOIN ... ON ... 文本生成) ← L363-460
  适配项（移植契约允许）：配置 get_settings() → 显式参数
  (join_path_in_prompt 默认 GRAPH_JOIN_PATH_IN_PROMPT=True,
   max_total 默认 GRAPH_EXPAND_MAX_TOTAL=10); 其余逐行保留
  (本文件在源仓库本就是纯同步实现, 无 async/租户/LLM 调用可改)。
"""
from __future__ import annotations

import logging

from domains.chatbi.graph_core import (
    GRAPH_EXPAND_MAX_TOTAL,
    GRAPH_JOIN_PATH_IN_PROMPT,
    SchemaGraph,
)
from domains.chatbi.models import SemanticModelContent

logger = logging.getLogger(__name__)


def get_schema_graph(content: SemanticModelContent | None,
                     **overrides) -> SchemaGraph:
    """构建 SchemaGraph 实例 (请求级工厂函数)。

    调用方在请求处理流程中调用一次, 将返回的 SchemaGraph 传给
    expand_with_relationships / build_join_path_section, 避免同一请求内重复构建。

    Args:
        content: 语义层内容 (None → 空图)
        overrides: SchemaGraph 构造参数透传(expand_max_total/
            expand_use_community/max_join_path_hops 等)——设置页阈值
            注入的通道, 未传时用图谱栈默认常量。

    Returns:
        SchemaGraph 实例。
    """
    sg = SchemaGraph(content, **overrides)
    logger.info(
        "🕸️ SchemaGraph 构建: %d 节点, %d 边",
        sg.node_count, sg.edge_count,
    )
    return sg


def expand_with_relationships(
    content: SemanticModelContent | None,
    selected_names: list[str],
    max_depth: int = 2,
    max_total: int | None = None,
    graph: SchemaGraph | None = None,
) -> list[str]:
    """沿着语义层关系定义扩展关联表。

    优先使用 SchemaGraph (最短路径 + 社区补全) 替代纯 BFS。
    SchemaGraph 构建失败时降级回原始 BFS (fail-open, 保证查询可用)。

    设计原则:
      - 用语义层结构化关系数据 (Relationship), 不硬编码命名规则
      - 语义层的关系由 _scan_relationships (外键) + knowledge_graph (LLM 推断) 产出,
        这里只消费不推断
      - 防扩散: 种子表 (selected_names) 永远保留, 不受上限影响
      - SchemaGraph 模式下按图距离排序 (近的优先), 不会因 BFS 遍历顺序导致
        超级枢纽的远亲占满名额

    为什么需要扩展:
      - 用户问题可能只提及一张表 (如"销售额"), 但 SQL 可能需要 JOIN 多张表
      - 扩展关联表后, LLM 在生成 SQL 时有更多表可选, 避免遗漏关联关系

    数据流:
      retriever 命中表 → expand_with_relationships → 扩展后的表名列表 →
      build_join_path_section (构建 JOIN 路径)

    防扩散机制:
      - max_total 上限 (默认 GRAPH_EXPAND_MAX_TOTAL, chat-bi 为 rag_max_schema_tables)
      - 种子表不受上限影响 (永远保留)
      - SchemaGraph 按图距离排序, 近的优先

    Args:
        content: 语义层内容 (含 relationships 定义)
        selected_names: 检索命中的表名
        max_depth: 关系扩展深度上限 (默认 2 跳; SchemaGraph 模式下按距离排序, 近的优先)
        max_total: 扩展后总表数上限 (None → GRAPH_EXPAND_MAX_TOTAL)
        graph: 请求级 SchemaGraph 单例 (None → 内部自建, 向后兼容)

    Returns:
        扩展后的表名列表 (含原始命中 + 关联表)
    """
    if content is None or not content.models:
        return list(selected_names)

    # 从配置读上限 (chat-bi 为 get_settings().rag_max_schema_tables)
    if max_total is None:
        max_total = GRAPH_EXPAND_MAX_TOTAL

    # 优先使用 SchemaGraph (最短路径 + 社区补全)
    try:
        sg = graph or get_schema_graph(content)
        if sg.node_count > 0:
            result = sg.expand_tables(selected_names, max_depth=max_depth, max_total=max_total)
            if result:
                added = set(result) - set(selected_names)
                logger.info(
                    "🕸️ 图谱表扩展: 种子 %d 张 → 扩展后 %d 张 (新增 %d: %s), 方法=SchemaGraph",
                    len(selected_names), len(result), len(added),
                    ",".join(sorted(added)) if added else "无",
                )
                return result
    except Exception as e:
        logger.warning(
            "expand_with_relationships: SchemaGraph 扩展失败, 降级为 BFS: %s", e,
        )

    # 降级: 原始 BFS 双向遍历
    result = _expand_with_bfs(content, selected_names, max_depth, max_total)
    added = set(result) - set(selected_names)
    logger.info(
        "🕸️ 图谱表扩展: 种子 %d 张 → 扩展后 %d 张 (新增 %d: %s), 方法=BFS(降级)",
        len(selected_names), len(result), len(added),
        ",".join(sorted(added)) if added else "无",
    )
    return result


def _expand_with_bfs(
    content: SemanticModelContent,
    selected_names: list[str],
    max_depth: int,
    max_total: int,
) -> list[str]:
    """原始 BFS 扩展 (降级备用)。

    BFS 按深度优先 (先 1 跳后 2 跳), 累计表数达到 max_total 后停止。
    超级枢纽 (如 uc_users 有 45 邻居) 不会把全库拉进来, 因为到达上限后
    后续邻居不再加入。种子表 (selected_names) 永远保留, 不受上限影响。

    设计决策:
      - 双向邻接表: 关系定义是单向的 (A→B), 但 JOIN 需要双向可达,
        所以构建双向邻接表确保 BFS 能从任一方向遍历
      - 深度优先 + 上限截断: 优先扩展 1 跳关系, 再扩展 2 跳关系,
        确保最相关的表先被包含

    降级触发条件:
      - SchemaGraph 构建失败 (如语义层数据异常)
      - SchemaGraph.expand_tables 返回空列表
    """
    # 构建双向邻接表 (正向: 表→关系目标; 反向: 被关系指向的表→源表)
    #
    # 为什么需要双向:
    #   语义层关系定义是单向的 (如 biz_orders → biz_users 表示 orders 引用了 users),
    #   但用户问题可能从任一表出发, 双向邻接确保 BFS 能遍历所有方向。
    #   例如用户问"用户信息"时, 从 biz_users 出发也能找到 biz_orders.
    adjacency: dict[str, set[str]] = {}
    for model in content.models:
        for rel in model.relationships:
            target = rel.target_model
            if not target:
                continue
            adjacency.setdefault(model.name, set()).add(target)
            adjacency.setdefault(target, set()).add(model.name)

    if not adjacency:
        return list(selected_names)

    result_set = set(selected_names)
    frontier = set(selected_names)
    for depth in range(max_depth):
        if len(result_set) >= max_total:
            break
        next_frontier = set()
        for table_name in frontier:
            if len(result_set) >= max_total:
                break
            for neighbor in adjacency.get(table_name, set()):
                if neighbor not in result_set and len(result_set) < max_total:
                    result_set.add(neighbor)
                    next_frontier.add(neighbor)
        if not next_frontier:
            break
        frontier = next_frontier

    if len(result_set) >= max_total:
        logger.warning(
            "expand_with_relationships(BFS): 达到表数上限 %d (种子 %d 张), 截断扩展 (深度=%d)",
            max_total, len(selected_names), max_depth,
        )

    return list(result_set)


def build_join_path_section(
    content: SemanticModelContent | None,
    table_names: list[str],
    graph: SchemaGraph | None = None,
    seed_names: list[str] | None = None,
    *,
    join_path_in_prompt: bool = GRAPH_JOIN_PATH_IN_PROMPT,
) -> str:
    """构建【JOIN 路径】prompt 块 (预计算 JOIN 路径 + ON 条件)。

    从 SchemaGraph 获取种子表之间的最短 JOIN 路径, 格式化为 LLM 可直接使用的
    JOIN 语句 ("t1 LEFT JOIN t2 ON ..." 文本), 减少 LLM 自行推断 JOIN 逻辑的负担。

    优化: 只对种子表 + 种子表的 1-hop 邻居计算 JOIN 路径,
    避免社区远亲产生大量无意义路径对 (C(n,2) 爆炸)。

    为什么需要预计算 JOIN 路径:
      - LLM 自行推断 JOIN 逻辑容易出错 (如选错 JOIN 列或 JOIN 类型)
      - 语义层的关系定义是明确的, 预计算后直接给 LLM 使用更可靠
      - 对标 AEE-001: 减少 LLM 推理负担, 把确定性的工作放在系统侧

    数据流:
      SchemaGraph → get_join_context → 格式化 JOIN 路径文本 →
      注入 T029 SQL 生成 prompt 的【JOIN 路径】段

    配置开关:
      join_path_in_prompt (chat-bi: config.graph_join_path_in_prompt, 本仓库
      为显式参数, 默认 GRAPH_JOIN_PATH_IN_PROMPT=True) — 可关闭此功能,
      让 LLM 自行推断

    Args:
        content: 语义层内容
        table_names: 扩展后的表名列表
        graph: 请求级 SchemaGraph 单例 (None → 内部自建, 向后兼容)
        seed_names: 检索命中的种子表名 (None → 退化为对全部 table_names 算路径)
        join_path_in_prompt: 是否注入 JOIN 路径块 (False → 返回空串)

    Returns:
        JOIN 路径文本块 (空字符串表示无法生成或配置关闭)
    """
    if not join_path_in_prompt:
        return ""

    if content is None or not table_names or len(table_names) < 2:
        return ""

    try:
        sg = graph or get_schema_graph(content)

        # 只对种子表 + 种子表的 1-hop 邻居计算 JOIN 路径
        # 社区远亲不需要 JOIN 路径 (LLM 不会用它们做 JOIN)
        if seed_names:
            seed_set = set(seed_names) & set(table_names)
            join_tables = set(seed_set)
            for s in seed_set:
                if s in sg._graph:
                    for nb in list(sg._graph.neighbors(s)) + list(sg._graph.predecessors(s)):
                        if nb in set(table_names):
                            join_tables.add(nb)
            join_paths = sg.get_join_context(list(join_tables))
        else:
            join_tables = set(table_names)
            join_paths = sg.get_join_context(table_names)
    except Exception as e:
        logger.warning("build_join_path_section: SchemaGraph 失败, 跳过 JOIN 路径块: %s", e)
        return ""

    if not join_paths:
        logger.info("🕸️ JOIN 路径: %d 张表, 未找到连通路径", len(table_names))
        return ""

    logger.info(
        "🕸️ JOIN 路径: %d 张表 (计算 %d 张), 预计算 %d 条路径 (%s)",
        len(table_names),
        len(join_tables) if seed_names else len(table_names),
        len(join_paths),
        " → ".join(p.tables[0] + ".." + p.tables[-1] for p in join_paths),
    )

    lines = ["涉及表之间的 JOIN 路径 (系统预计算, 请直接使用):"]
    for path in join_paths:
        # 格式: table1 JOIN table2 ON ... JOIN table3 ON ...
        parts = [path.tables[0]]
        for i in range(len(path.on_conditions)):
            join_type = path.join_types[i] if i < len(path.join_types) else "LEFT"
            on = path.on_conditions[i]
            conf = path.confidences[i] if i < len(path.confidences) else 0.0
            parts.append(f"{join_type} JOIN {path.tables[i + 1]} ON {on}  (confidence={conf:.1f})")
        lines.append("  " + " ".join(parts))

    # 检查无路径的表 (只检查参与计算的表, 不报社区远亲)
    connected_tables: set[str] = set()
    for path in join_paths:
        connected_tables.update(path.tables)
    check_tables = set(join_tables) if seed_names else set(table_names)
    disconnected = check_tables - connected_tables
    if disconnected:
        lines.append(f"  以下表无直接关联路径: {', '.join(sorted(disconnected))}")

    return "\n".join(lines)
