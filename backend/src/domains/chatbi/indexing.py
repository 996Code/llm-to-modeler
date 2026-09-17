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
import re
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
    register_chunk_identities,
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
    doc_id: str = DOC_SCHEMA,
    revision: Optional[int] = None,
) -> IndexResult:
    """把 SemanticModelContent 索引进向量库。

    Args:
        content: 语义层内容 (models + metrics)
        data_source_id: 数据源 id (进 record metadata, 供调试/多源追溯)
        store: ChatBIVectorStore (SDK MilvusVectorStore 适配壳)
        embedder: ChatBIEmbedder (llm.embeddings 底座)
        db: pack 关系库 (scope 未显式给出时用于解析/签发数据源 scope_id)
        scope: 显式指定的物理 scope (优先于 db 解析;测试直连用)
        doc_id: 目标分区(十七审 7.7: revision 构建写 "schema_r{version}",
            读者读 active 指针指向的分区;缺省 legacy "schema")

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

    # 收集所有可索引对象 → (id, type, name, owner_model, text)
    # id 命名规则: <data_source_id>:<type>[:<owner_model>]:<name>
    # 物理 chunk_id 由适配层编码(type/owner/rev 进主键, 见 stores._chunk_id):
    #   十八审 P1 6.3: metric 必须带 owner model——跨表同名指标否则互相覆盖
    items: list[tuple[str, str, str, Optional[str], str]] = []
    for model in content.models:
        text = model_to_text(model)
        rid = f"{data_source_id}:model:{model.name}"
        items.append((rid, "model", model.name, None, text))
        # metric 依附于 model, 以 model 为单位组织(owner 进身份)
        for metric in model.metrics:
            mtext = metric_to_text(metric)
            mid = f"{data_source_id}:metric:{model.name}:{metric.name}"
            items.append((mid, "metric", metric.name, model.name, mtext))

    texts = [t for _, _, _, _, t in items]

    # 批量 embed
    # 一次调用 embedder.embed 批量处理所有文本, 比逐条调用快
    # 但也意味着如果某个文本 embed 失败, 全部失败
    try:
        vectors = embedder.embed(texts)
    except Exception as e:
        logger.warning("build_index embed 失败, 跳过索引: %s", e)
        return IndexResult(indexed_count=0, error=str(e))

    # 组装 VectorRecord + 批量 upsert
    # metadata 中包含 data_source_id/type/name/owner_model/rev,
    # 供适配层主键编码与检索结果定位(owner_model 让纯指标命中直接归表)
    records = []
    for (rid, rtype, name, owner, text), vec in zip(items, vectors):
        meta = {
            "data_source_id": data_source_id,
            "type": rtype,
            "name": name,
        }
        if owner:
            meta["owner_model"] = owner
        if revision is not None:
            meta["rev"] = revision
        records.append(VectorRecord(
            id=rid,
            vector=vec,
            metadata=meta,
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
        store.upsert_records(scope, records, doc_id=doc_id)
    except Exception as e:
        logger.warning("build_index upsert 失败, 跳过索引: %s", e)
        return IndexResult(indexed_count=0, error=str(e))

    # 十九审 6.1: 身份真源落 PG——超长 chunk_id 截断后业务身份以
    # chatbi_chunk_identities 反查恢复, 不再依赖不可逆主键解码
    register_chunk_identities(db, scope, records, doc_id)

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
    十七审 7.7: 带 expected_version 时走 revision 发布——新内容写独立
    分区, 成功后原子翻转 active 指针(更高 version 才能翻转), 消灭
    delete-first 空窗和多 worker 交错覆盖。
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
        result = rebuild_index(content, data_source_id, store, embedder, db,
                               revision=expected_version)
        # 构建后复查: 构建期间 current 变了 → 用最新版补建一次
        _, cur_v = semantic.load_content(db, data_source_id)
        if cur_v is not None and expected_version is not None and cur_v != expected_version:
            from domains.chatbi import semantic as _sem
            latest_content, _ = _sem.load_content(db, data_source_id)
            if latest_content is not None:
                result = rebuild_index(latest_content, data_source_id,
                                       store, embedder, db, revision=cur_v)
        return result


def rebuild_index(
    content: SemanticModelContent,
    data_source_id: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
    revision: Optional[int] = None,
    published_grace_seconds: int = 600,
    unfinished_ttl_seconds: int = 86400,
) -> RebuildResult:
    """语义层变更后重建索引。

    Args:
        content: 新的语义层内容
        data_source_id: 数据源 id (按源删建, 非全库)
        store: ChatBIVectorStore
        embedder: ChatBIEmbedder
        db: pack 关系库 (scope 未显式给出时用于解析/签发数据源 scope_id)
        scope: 显式指定的物理 scope (优先于 db 解析;测试直连用)
        revision: 语义版本号(十七审 7.7)——非 None 时走 revision 发布:
            全量写入独立分区 schema_r{revision} → 原子翻转 active 指针
            (仅更高版本可翻转) → 延迟清理旧分区。None 走 legacy
            delete-first(兼容无版本调用方)。

    Returns:
        RebuildResult(deleted_count, indexed_count) — 失败时 error 非空 (不抛)

    revision 发布的正确性:
      - 构建中途失败 → 指针未动, 读者继续读旧分区(无空窗);
      - 两个 worker 并行 → 各写各的版本分区(物理不交错), 指针由
        版本守卫保证最终指向最新完成者;
      - 旧任务晚于新任务完成 → 低版本翻转被拒, 不覆盖已发布新版。
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

    # ── 十七审 7.7: revision 发布路径(带版本号的调用方) ──
    if revision is not None:
        return _rebuild_with_revision(
            content, data_source_id, store, embedder, scope, db, revision,
            published_grace_seconds=published_grace_seconds,
            unfinished_ttl_seconds=unfinished_ttl_seconds)

    # ── legacy 路径: 删旧 + 建新(无版本号的兼容调用方) ──
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
            db=db,
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


def _rebuild_with_revision(
    content: SemanticModelContent,
    data_source_id: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    scope: str,
    db: Optional[PackRelationalDB],
    revision: int,
    published_grace_seconds: int = 600,
    unfinished_ttl_seconds: int = 86400,
) -> RebuildResult:
    """revision namespace 发布: 建新分区 → 原子翻转指针 → 延迟清理旧分区。

    十八审 P0-2: 物理隔离由 chunk_id 主键保证(revision + owner model 进键,
    见 stores._chunk_id)——同一对象在新旧 revision 是不同主键行, upsert 新
    分区不再触碰旧分区; 指针翻转失败时旧 active 分区完好无损。

    清理策略(十八审 6.7): 不在翻转后立即删上一分区(读到旧指针的在途查询
    可能撞上删除)——保留两代(active + prev), 发布 rN 时只 GC version ≤ N-2
    的分区; 构建意图先落 chatbi_index_builds(building), 崩溃残余的半成品
    分区也由同一 GC 回收(按记录的 doc_id 删)。
    """
    from domains.chatbi.stores import (get_active_doc_id, set_active_doc_id,
                                       record_index_build, gc_index_builds,
                                       _ensure_build_ledger)

    prev_doc = get_active_doc_id(db, scope) if db is not None else None
    new_doc = f"{DOC_SCHEMA}_r{revision}"

    # 十九审 6.5: 升级回填——active 指针已存在(如 schema_r17)但台账无行时
    # 补一条 published, 否则旧 revision 向量永远无法被 GC
    if db is not None and prev_doc:
        m = re.match(rf"^{re.escape(DOC_SCHEMA)}_r(\d+)$", prev_doc or "")
        if m:
            try:
                _ensure_build_ledger(db, scope, int(m.group(1)), prev_doc,
                                     status="published")
            except Exception as e:
                logger.warning("升级回填台账失败(scope=%s): %s", scope[:8], e)

    # 1. 构建意图落账(GC 依据: 崩溃/让路的半成品分区按 doc_id 可回收)
    if db is not None:
        try:
            record_index_build(db, scope, revision, new_doc, status="building")
        except Exception as e:
            logger.warning("rebuild_index(revision) 构建意图落账失败: %s", e)

    # 2. 全量构建到独立分区(物理主键含 revision, 不碰任何现存分区)
    try:
        built = build_index(
            content=content, data_source_id=data_source_id,
            store=store, embedder=embedder, db=db, scope=scope,
            doc_id=new_doc, revision=revision)
    except Exception as e:
        logger.warning("rebuild_index(revision) 构建失败(指针未动): %s", e)
        return RebuildResult(error=str(e))
    if built.error:
        logger.warning("rebuild_index(revision) 构建降级(指针未动): %s",
                       built.error)
        return RebuildResult(error=built.error)

    # 3. 原子翻转 active 指针(仅更高 version 生效)
    if db is None:
        logger.warning("rebuild_index(revision) 无 db 无法翻转指针, "
                       "新分区 %s 未发布", new_doc)
        return RebuildResult(error="revision 发布需要 db 写 active 指针")
    try:
        became_active = set_active_doc_id(db, scope, new_doc, revision)
    except Exception as e:
        logger.warning("rebuild_index(revision) 指针翻转失败(旧分区完好): %s", e)
        return RebuildResult(indexed_count=built.indexed_count,
                             error=f"active 指针翻转失败: {e}")

    if not became_active:
        # 晚完成的旧任务: 已有更新 revision 发布, 本次让路。
        # 分区留待 GC(不立即删——另一 worker 可能刚发布了同版本分区)。
        try:
            record_index_build(db, scope, revision, new_doc, status="yielded")
        except Exception:
            pass
        logger.info("rebuild_index(revision) v%s 让路(已有更新版本发布)",
                    revision)
        return RebuildResult(indexed_count=built.indexed_count)

    # 4. 发布成功: 标记 + 两代 grace GC(旧 active 的在途读者不受影响)
    deleted = 0
    try:
        record_index_build(db, scope, revision, new_doc, status="published")
        # 首次 revision 发布时, legacy "schema" 无版本分区按 version=0 落账
        if prev_doc == DOC_SCHEMA:
            record_index_build(db, scope, 0, DOC_SCHEMA, status="published")
        deleted = gc_index_builds(db, scope, store, keep_generations=2,
                                  published_grace_seconds=published_grace_seconds,
                                  unfinished_ttl_seconds=unfinished_ttl_seconds)
    except Exception as e:
        logger.info("rebuild_index(revision) 旧分区 GC 失败(无害, 下次重试): %s", e)

    logger.info(
        "rebuild_index(revision): v%s 分区 %s 发布 (GC %d, 建 %d, ds=%s)",
        revision, new_doc, deleted, built.indexed_count, data_source_id,
    )
    return RebuildResult(deleted_count=deleted,
                         indexed_count=built.indexed_count)
