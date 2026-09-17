"""chatbi 索引构建 —— SemanticModelContent → 向量索引(全量 + 重建)。

【移植来源】自 chat-bi backend/app/services/ 忠实移植:
  - indexer.py(T020):model_to_text / metric_to_text 文本序列化 +
    build_index(收集 → 批量 embed → upsert,幂等);
  - indexer_update.py(T021):rebuild_index"删旧建新"模式
    (按数据源维度删建,非逐条 diff——变更频率低,全量重建成本可接受);
  - 文本序列化细节 1:1 保留:中文描述重复 2 遍提升语义权重、display_name
    优先、列只取中文 display_name、data_type 不进文本、指标公式是匹配关键。

【与本仓库设施的对接差异】(语义等价)
  - 向量库:SDK MilvusVectorStore(经 stores.ChatBIVectorStore 适配),
    每数据源一个 scope → collection chatbi_{scope}_v1;
    源实现的 delete_by_filter({"data_source_id"}) → delete_doc(scope, DOC_SCHEMA)。
  - Embedder:ChatBIEmbedder(llm.embeddings 底座,批量一次调用);
  - 同步实现(调用方是同步线程池)。

失败降级纪律与源一致:embed/upsert 失败不阻塞元数据操作,
返回 error 非空的结果对象而不抛异常。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domains.chatbi.models import Metric, Model, SemanticModelContent
from domains.chatbi.stores import (
    DOC_SCHEMA,
    ChatBIEmbedder,
    ChatBIVectorStore,
    PackRelationalDB,
    VectorRecord,
    get_or_create_scope,
)

logger = logging.getLogger(__name__)


# ── 文本序列化: 语义对象 → 可 embed 的文本 ────────────────────

def model_to_text(model: Model) -> str:
    """Model → embed 文本(中文优先,重复描述增强语义)。

    BGE 等中文向量模型对中文文本质量高度敏感。
    设计原则:
      - 中文描述重复2遍, 提升核心语义权重
      - display_name (中文名) 优先, 英文表名放后面
      - 列信息只保留 display_name (中文注释), 不放英文列名
      - data_type 不进文本 (对语义匹配无帮助, 反增噪声)

    注意事项:
    - 重复描述: BGE 模型对重复文本的语义权重有叠加效果,
      但重复过多会稀释区分度。2 遍是经验值, 经测试效果最佳。
    - 英文表名: 向量模型对英文不敏感, 但保留给精确匹配场景。
      格式 "表名{name}" 明确标识这是表名, 帮助模型区分。
    - 列信息: 只取有 display_name 的列, 没有 display_name 的列
      用英文列名对语义匹配无帮助, 跳过。
    """
    parts: list[str] = []
    # 中文描述重复2遍 — 核心语义
    if model.description:
        parts.append(model.description)
        parts.append(model.description)
    # display_name (中文名)
    # 如果 display_name 和 name 相同, 说明没有中文别名, 不重复添加
    if model.display_name and model.display_name != model.name:
        parts.append(model.display_name)
    # 英文表名放最后 (向量模型对英文不敏感, 但保留给精确匹配)
    parts.append(f"表名{model.name}")
    # 列: 只取中文 display_name, 不放英文列名
    col_names: list[str] = []
    for col in model.columns:
        if col.display_name and col.display_name != col.name:
            col_names.append(col.display_name)
    if col_names:
        parts.append("字段:" + ",".join(col_names))
    return "。".join(parts)


def metric_to_text(metric: Metric) -> str:
    """Metric → embed 文本 (中文优先, display_name + 描述 + 公式)。

    指标文本化: 包含描述、显示名、条件、指标名、公式。
    公式是核心: 指标查询时, 用户问 "销售额", 公式 sum(amount) 是匹配关键。
    condition 可选: 如 "status=1" 这种过滤条件, 帮助区分同名指标。
    """
    parts: list[str] = []
    if metric.description:
        parts.append(metric.description)
    if metric.display_name and metric.display_name != metric.name:
        parts.append(metric.display_name)
    if metric.condition:
        parts.append(metric.condition)
    parts.append(f"指标{metric.name}")
    parts.append(metric.formula)
    return "。".join(parts)


# ── 索引构建 ──────────────────────────────────────────────────

@dataclass
class IndexResult:
    """build_index 结果。"""
    indexed_count: int = 0
    error: Optional[str] = None


def build_index(
    content: SemanticModelContent,
    data_source_id: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
) -> IndexResult:
    """把 SemanticModelContent 索引进向量库。

    Args:
        content: 语义层内容 (models + metrics)
        data_source_id: 数据源 id (进 record metadata, 供调试/多源追溯)
        store: ChatBIVectorStore (SDK MilvusVectorStore 适配壳)
        embedder: ChatBIEmbedder (llm.embeddings 底座)
        db: pack 关系库 (scope 未显式给出时用于解析/签发数据源 scope_id)
        scope: 显式指定的物理 scope (优先于 db 解析;测试直连用)

    Returns:
        IndexResult(indexed_count) — 失败时 indexed_count=0, error 非空 (不抛)

    设计:
      - 先收集所有文本 → 批量 embed (一次调用, 对标效率)
      - 失败降级: 返回 0, 不阻塞调用方 (扫描/CRUD)

    数据流:
    content.models → model_to_text (每个 model) → items (含 model + metric)
    items → 批量 embed → vectors → 组装 VectorRecord → upsert

    幂等性:
    同 data_source_id + model/metric name 生成相同 id
    (scope 内 chunk_id = "type:name"), upsert 会覆盖旧向量,
    不会产生重复记录。所以重复调用 build_index 是安全的 (全量重建场景)。

    失败降级:
    - embed 失败: 不建索引, 不影响元数据操作
    - upsert 失败: 同上, 不影响元数据操作
    调用方 (扫描/CRUD) 不应依赖索引构建成功。
    """
    if not content.models:
        return IndexResult(indexed_count=0)

    # 收集所有可索引对象 → (id, type, name, text)
    # id 命名规则: <data_source_id>:<type>:<name>
    # 确保全局唯一;物理 chunk_id 由适配层编码为 "type:name"
    # (scope 已隔离数据源, 见 stores.ChatBIVectorStore._chunk_id)
    items: list[tuple[str, str, str, str]] = []
    for model in content.models:
        text = model_to_text(model)
        rid = f"{data_source_id}:model:{model.name}"
        items.append((rid, "model", model.name, text))
        # metric 依附于 model, 以 model 为单位组织
        for metric in model.metrics:
            mtext = metric_to_text(metric)
            mid = f"{data_source_id}:metric:{metric.name}"
            items.append((mid, "metric", metric.name, mtext))

    texts = [t for _, _, _, t in items]

    # 批量 embed
    # 一次调用 embedder.embed 批量处理所有文本, 比逐条调用快
    # 但也意味着如果某个文本 embed 失败, 全部失败
    try:
        vectors = embedder.embed(texts)
    except Exception as e:
        logger.warning("build_index embed 失败, 跳过索引: %s", e)
        return IndexResult(indexed_count=0, error=str(e))

    # 组装 VectorRecord + 批量 upsert
    # metadata 中包含 data_source_id 和 type, 供适配层编码与结果展示
    records = []
    for (rid, rtype, name, text), vec in zip(items, vectors):
        records.append(VectorRecord(
            id=rid,
            vector=vec,
            metadata={
                "data_source_id": data_source_id,
                "type": rtype,
                "name": name,
            },
            text=text,  # 原文保留, 调试/缓存用
        ))

    # 物理 scope: 显式参数 > 数据源行登记(缺失则签发)
    if scope is None:
        if db is None:
            msg = "build_index 需要 db 或显式 scope 以定位向量 collection"
            logger.warning("build_index scope 缺失: %s", msg)
            return IndexResult(indexed_count=0, error=msg)
        try:
            scope = get_or_create_scope(db, data_source_id)
        except Exception as e:
            # scope 签发失败(数据源行不存在等)→ 降级不抛(源契约: 不阻塞调用方)
            logger.warning("build_index scope 解析失败: %s", e)
            return IndexResult(indexed_count=0, error=str(e))

    try:
        store.upsert_records(scope, records, doc_id=DOC_SCHEMA)
    except Exception as e:
        logger.warning("build_index upsert 失败, 跳过索引: %s", e)
        return IndexResult(indexed_count=0, error=str(e))

    logger.info("build_index: 索引 %d 条 (data_source=%s)", len(records), data_source_id)
    return IndexResult(indexed_count=len(records))


# ── 索引重建(删旧 + 建新)────────────────────────────────────────

@dataclass
class RebuildResult:
    """rebuild_index 结果。"""
    deleted_count: int = 0
    indexed_count: int = 0
    error: Optional[str] = None


# 十审 7.2: datasource 级索引构建串行锁——同一数据源的 delete-then-build
# 不允许交错(否则旧版构建可在新版之后发布)
_index_build_locks: dict = {}
_index_build_guard = __import__('threading').Lock()

def _get_build_lock(ds_id: str):
    import threading
    with _index_build_guard:
        if ds_id not in _index_build_locks:
            _index_build_locks[ds_id] = threading.Lock()
        return _index_build_locks[ds_id]


def guarded_rebuild(content, data_source_id, store, embedder, db,
                    expected_version=None):
    """带 datasource 级串行锁和版本前后复查的 rebuild_index 包装。

    十审 7.2: delete-then-build 无锁时, 旧版构建可在新版之后完成并
    覆盖已发布的新版。串行锁 + 构建前版本校验保证最终发布最新版。
    """
    from domains.chatbi import semantic
    lock = _get_build_lock(data_source_id)
    with lock:
        # 构建前校验: expected_version 不匹配说明有更新版本, 旧构建让路
        if expected_version is not None:
            _, cur_v = semantic.load_content(db, data_source_id)
            if cur_v is not None and cur_v != expected_version:
                return type('R', (), {'error': f'跳过过时构建 v{expected_version} (current v{cur_v})',
                                      'deleted_count': 0, 'indexed_count': 0})()
        result = rebuild_index(content, data_source_id, store, embedder, db)
        # 构建后复查: 构建期间 current 变了 → 用最新版补建一次
        _, cur_v = semantic.load_content(db, data_source_id)
        if cur_v is not None and hasattr(content, 'version') and            getattr(content, 'version', 0) != cur_v:
            from domains.chatbi import semantic as _sem
            latest_content, _ = _sem.load_content(db, data_source_id)
            if latest_content is not None:
                result = rebuild_index(latest_content, data_source_id,
                                       store, embedder, db)
        return result


def rebuild_index(
    content: SemanticModelContent,
    data_source_id: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
) -> RebuildResult:
    """语义层变更后重建索引 (删旧 + 建新)。

    Args:
        content: 新的语义层内容
        data_source_id: 数据源 id (按源删建, 非全库)
        store: ChatBIVectorStore
        embedder: ChatBIEmbedder
        db: pack 关系库 (scope 未显式给出时用于解析/签发数据源 scope_id)
        scope: 显式指定的物理 scope (优先于 db 解析;测试直连用)

    Returns:
        RebuildResult(deleted_count, indexed_count) — 失败时 error 非空 (不抛)

    流程:
      1. 删该数据源 DOC_SCHEMA 分区的全部旧索引
         (源实现 delete_by_filter({"data_source_id"}) 的 per-scope 等价物)
      2. 用新 content 重建 (build_index)
      失败任一步 → 降级返回, 不阻塞调用方

    为什么删建而非 diff:
      - diff 需要 content 版本对比, 复杂且易错 (增删改列/指标/关系组合)
      - 删全量 + 重建语义清晰, 对标 RAG-001 "更新索引"
      - 触发点少 (回滚/编辑), 性能不是瓶颈

    边界情况:
    - 数据源没有旧索引 (首次重建): 删除返回 0, 不影响建新
    - 删除失败: 不继续建新 (避免重复记录), 返回错误
    - 建新失败: 旧索引已删除, 该数据源暂时没有索引 (空窗期)
      这是设计接受的降级行为: 用户下次手动触发索引重建即可修复。
    """
    # 物理 scope 解析(与 build_index 同规则)
    if scope is None:
        if db is None:
            msg = "rebuild_index 需要 db 或显式 scope 以定位向量 collection"
            logger.warning("rebuild_index scope 缺失: %s", msg)
            return RebuildResult(error=msg)
        try:
            scope = get_or_create_scope(db, data_source_id)
        except Exception as e:
            logger.warning("rebuild_index scope 解析失败: %s", e)
            return RebuildResult(error=str(e))

    # 1. 删旧: 该 scope 的语义层分区(同数据源维度, few-shot 分区不受影响)
    #    首次建索引时 collection 尚不存在——Milvus 抛 collection not found,
    #    视为"无旧索引可删"(docstring 边界情况第一条), 继续建新。
    #    此前把该异常当删除失败直接 return, 导致首次扫描永远建不出索引。
    try:
        deleted = store.delete_doc(scope, DOC_SCHEMA)
    except Exception as e:
        if "not found" in str(e).lower() or "100" in str(e):
            deleted = 0  # collection 不存在 = 无旧索引
            logger.info("rebuild_index: collection 尚不存在(首次建), 跳过删旧")
        else:
            logger.warning("rebuild_index 删旧索引失败: %s", e)
            return RebuildResult(error=str(e))

    # 2. 建新: 用新 content 重建索引
    try:
        built = build_index(
            content=content,
            data_source_id=data_source_id,
            store=store,
            embedder=embedder,
            scope=scope,
        )
    except Exception as e:
        # build_index 内部已降级 (不抛异常), 这里兜底
        # 注意: 旧索引已删除, 如果建新失败, 数据源暂时没有索引
        logger.warning("rebuild_index 建新索引失败 (旧索引已删): %s", e)
        return RebuildResult(deleted_count=deleted, error=str(e))

    logger.info(
        "rebuild_index: 删 %d + 建 %d (data_source=%s)",
        deleted, built.indexed_count, data_source_id,
    )
    return RebuildResult(
        deleted_count=deleted,
        indexed_count=built.indexed_count,
        error=built.error,  # build_index 可能部分失败 (error 非空)
    )
