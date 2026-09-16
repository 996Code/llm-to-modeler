"""chatbi 后台任务 handler(平台任务框架;pack 经 register_tasks 注册)。

任务类型:
  chatbi.scan_datasource   数据源扫描(信息模式内省→LLM 富化→语义层版本落库),
                           带 scan_status/progress 进度回写(数据源行)
  chatbi.refresh_semantics 语义层自动刷新(按 settings.metadata_refresh_hours 调度;
                           引擎任务框架当前无定时器,由 api 的手动触发 + 宿主
                           调度接入,handler 随时可用)

移植来源: chat-bi backend/app/services/metadata_refresher.py (307 行) +
semantic_scanner 的进度回写机制。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PACK_NAME = "chatbi"


def register_tasks(manager, app_state=None) -> None:
    """向平台任务框架注册本 pack 的任务 handler。

    app_state 经闭包捕获(模式照抄 knowledge_graph/tasks.py)——
    TaskHandle 只有 payload/进度上报, 平台组件(LLM 客户端)从装配期捕获。

    另起元数据定时刷新守护线程(对标源 APScheduler 每 6h):
    周期 = 设置 metadata_refresh_hours(0 = 关闭, 缺省 6);每轮经任务框架
    submit 走统一通道(去重/任务中心可观测), 不直接调 handler。"""
    def _scan(handle):
        return _task_scan_datasource(handle, app_state)

    def _refresh(handle):
        return _task_refresh_semantics(handle, app_state)

    def _consolidate(handle):
        payload = handle.payload or {}
        return _task_consolidate_memories(
            handle, app_state, data_source_id=payload.get("data_source_id"))

    manager.register("chatbi.scan_datasource", _scan, pack_name=PACK_NAME)
    manager.register("chatbi.refresh_semantics", _refresh, pack_name=PACK_NAME)
    manager.register("chatbi.memory.consolidate", _consolidate, pack_name=PACK_NAME)
    logger.info("chatbi tasks registered: scan_datasource / refresh_semantics / memory.consolidate")
    _start_refresh_scheduler(manager, app_state)


_refresh_thread = None
_refresh_stop = None


def _start_refresh_scheduler(manager, app_state) -> None:
    """元数据定时刷新线程(进程级单例;unload 由 pack 生命周期终止进程,
    线程随进程退出——平台无 pack 级线程取消钩子, daemon 即可)。"""
    global _refresh_thread, _refresh_stop
    if _refresh_thread is not None and _refresh_thread.is_alive():
        return
    import threading

    _refresh_stop = threading.Event()

    def _loop():
        # 双周期任务: 元数据刷新(metadata_refresh_hours, 缺省 6h, 0=关) +
        # 健康巡检(5min, 对标源 APScheduler datasource_health 间隔)。
        # 周期每轮重读设置(管理端热改即时生效);检查间隔取 5min 粒度。
        _loop._last_refresh = _now_ts()   # 启动即视为已刷新(避免重启风暴)
        _loop._last_health = 0.0
        while not _refresh_stop.wait(300):
            now = _now_ts()
            # 健康巡检: 固定 5min(源 scheduler.py:94-102 同款间隔)
            if now - _loop._last_health >= 300:
                _loop._last_health = now
                try:
                    from domains.chatbi import datasources as ds_mod
                    from domains.chatbi.runtime import get_pack_db
                    ds_mod.check_all_health(get_pack_db())
                except Exception as e:
                    logger.warning("健康巡检失败(下轮重试): %s", e)
            # 元数据刷新: 设置周期
            try:
                hours = float(_load_settings(app_state).get("metadata_refresh_hours", 6))
            except Exception:
                hours = 6
            if hours <= 0:
                continue
            if now - _loop._last_refresh < hours * 3600:
                continue
            _loop._last_refresh = now
            try:
                manager.submit("chatbi.refresh_semantics", payload={},
                               dedupe_key="chatbi:refresh:all")
                logger.info("元数据定时刷新已提交 (周期 %gh)", hours)
            except Exception as e:
                logger.warning("元数据定时刷新提交失败: %s", e)

    _refresh_thread = threading.Thread(target=_loop, name="chatbi-metadata-refresh",
                                       daemon=True)
    _refresh_thread.start()
    logger.info("chatbi scheduler started: health 5min + metadata refresh (from settings)")


def _now_ts() -> float:
    import time
    return time.time()


def _task_scan_datasource(handle, app_state=None) -> dict:
    """扫描任务入口。payload: {datasource_id, rescan: bool}。"""
    from domains.chatbi import runtime, semantic, datasources
    payload = handle.payload or {}
    ds_id = payload.get("datasource_id")
    if not ds_id:
        raise ValueError("payload 缺 datasource_id")

    db = runtime.get_pack_db()
    llm = runtime.get_llm(app_state) if app_state else None
    info = datasources.get_datasource(db, ds_id, decrypt=True)
    if not info:
        raise ValueError(f"数据源不存在: {ds_id}")

    settings = _load_settings(app_state)

    def progress(pct: int, stage: str):
        # 双路进度: 任务中心(handle.set_progress) + 数据源行(ChatBI 语义)
        try:
            handle.set_progress(pct, stage)
        except Exception:
            pass
        datasources.update_datasource(db, ds_id, scan_progress=pct, scan_stage=stage)

    handle.log(f"开始扫描数据源: {info.name} ({info.db_type} {info.host}:{info.port}/{info.database})")
    datasources.update_datasource(db, ds_id, scan_status="scanning",
                                  scan_progress=0, scan_stage="开始扫描", scan_error="")
    try:
        content = semantic.scan_datasource(
            llm=llm, db=db, connect_info=_connect_info(info),
            infer_metrics=bool(settings.get("scan_metric_inference", True)),
            progress_cb=progress, datasource_id=ds_id)
        # 向量索引重建 (对标 _run_scan_background 尾部 rebuild_index;
        # 失败降级不阻塞——RAG 检索可用全表降级路径)
        indexed = 0
        try:
            from domains.chatbi import indexing, stores
            store = stores.get_vector(app_state)
            embedder = stores.get_embedder(llm)
            rb = indexing.rebuild_index(content, ds_id, store, embedder, db=db)
            indexed = rb.indexed_count
        except Exception as e:
            logger.warning("向量索引重建失败(降级, 不阻塞扫描): %s", e)
        datasources.update_datasource(
            db, ds_id, scan_status="done", scan_progress=100,
            scan_stage=f"完成: {len(content.models)} 张表, 索引 {indexed} 条",
            scanned_at=_now_iso())
        handle.log(f"扫描完成: {len(content.models)} 张表, 向量索引 {indexed} 条")
        return {"models": len(content.models), "indexed": indexed,
                "datasource_id": ds_id}
    except Exception as e:
        datasources.update_datasource(db, ds_id, scan_status="failed",
                                      scan_error=str(e)[:500])
        raise


def _task_consolidate_memories(handle, app_state=None, data_source_id=None) -> dict:
    """记忆整理任务入口(对标源 POST /memory/consolidate)。

    LLM 合并去重碎片记忆;原记忆标记 consolidated 隐藏(可追溯),
    linkage 结构化类型跳过。进度经任务中心 SSE 透出;关键节点写任务日志
    (任务中心"日志"页签不再空白)。

    闭环接线(复核报告 P0): 整理完成后按涉及的每个数据源无条件调用
    sync_linkage_to_graph——linkage 共现反哺图谱不再只藏在元数据刷新里;
    同步失败记入任务日志与结果(degraded 可见), 不静默。

    data_source_id: 只整理该数据源的记忆(None = 全部, 仍按 scope 分组)。
    """
    from domains.chatbi import memory, runtime
    llm = runtime.get_llm(app_state) if app_state else None
    if llm is None:
        raise ValueError("LLM 客户端不可用——记忆整理需要 LLM(检查插件依赖配置)")
    db = runtime.get_pack_db()

    def progress(pct: int, stage: str):
        try:
            handle.set_progress(pct, stage)
        except Exception:
            pass

    scope_text = f" (仅数据源 {data_source_id})" if data_source_id else ""
    handle.log(f"记忆整理开始{scope_text}: 按 用户+数据源 分组, 过滤已整理/结构化(linkage)条目")
    result = memory.consolidate_memories(
        llm, db, on_progress=progress, data_source_id=data_source_id)
    # 整理结果写任务日志(用户在任务中心能直接看到"整理了几条/合并成几条")
    detail = result.get("detail") or ""
    handle.log(
        f"整理完成: 合并为 {result.get('consolidated', 0)} 条精炼记忆, "
        f"参与 {result.get('total', 0)} 条原始记忆。{detail}")
    if result.get("consolidated"):
        handle.log("原始记忆已标记为'已整理'(默认隐藏, 记忆页勾选'显示已整理'可查看)")

    # ── linkage → 图谱反哺: 整理涉及的每个数据源都同步一次 ──
    # scopes 只含"产出了新记忆"的组;无产出时该数据源没有变化, 无需同步。
    graph_sync = []
    involved_ds = {s.get("data_source_id") for s in result.get("scopes", [])
                   if s.get("data_source_id")}
    if data_source_id:
        involved_ds = {d for d in involved_ds if d == data_source_id} or involved_ds
    for ds_id in sorted(involved_ds):
        try:
            from domains.chatbi.memory import get_memory_store
            from domains.chatbi.graph_infer import sync_linkage_to_graph
            res = sync_linkage_to_graph(
                db, get_memory_store(db), ds_id,
                rebuild_index=_make_index_rebuilder(app_state, ds_id))
            handle.log(f"linkage 图谱同步完成 (ds={ds_id[:8]}…): {res}")
            graph_sync.append({"data_source_id": ds_id, "ok": True, "result": res})
        except Exception as e:
            logger.warning("整理后 linkage 图谱同步失败 (ds=%s): %s", ds_id, e)
            handle.log(f"linkage 图谱同步失败 (ds={ds_id[:8]}…, 降级): {e}")
            graph_sync.append({"data_source_id": ds_id, "ok": False, "error": str(e)[:200]})
    result["graph_sync"] = graph_sync
    return result


def _task_refresh_semantics(handle, app_state=None) -> dict:
    """自动刷新入口(对标源 metadata_refresher.refresh_all_metadata):

    与全量扫描(scan_datasource)的语义差异:
      - 只做结构内省(llm=None), 不跑 LLM 富化——刷新是高频低价值操作,
        LLM 非确定性输出会产伪版本 + 全量调用成本;
      - 保留人工/LLM 标注(_merge_content: 新结构权威, 旧标注优先);
      - 跳过从未扫描过的数据源(首次扫描必须手动——需要 LLM 富化);
      - 内容指纹未变 → save_content 幂等不落新版本;
      - 落了新版本 → 重建向量索引(版本与索引不漂移)。
    """
    from domains.chatbi import runtime, semantic, datasources, indexing
    db = runtime.get_pack_db()
    settings = _load_settings(app_state)
    results = []
    for info in datasources.list_datasources(db, active_only=True):
        try:
            current = semantic.load_current_content(db, info.id)
            if current is None:
                # 从未扫描过: 首次扫描必须手动(需要 LLM 富化), 跳过
                results.append({"datasource_id": info.id, "ok": True,
                                "skipped": "never_scanned"})
                continue
            # 纯结构扫描(无 LLM, 不落库) → 合并旧标注/指标/关系 → 一次落版本。
            # persist=False: scan_datasource 内部 save 会先落一个"裸结构"
            # 中间版本——指纹基准被污染(每周期净增 2 版本), 且裸结构短暂
            # 成为 is_current(用户此刻查询丢失全部中文标注)。
            new_content = semantic.scan_datasource(
                llm=None, db=db, connect_info=_connect_info(info),
                infer_metrics=bool(settings.get("scan_metric_inference", True)),
                datasource_id=info.id, persist=False)
            merged = _merge_content(current, new_content)
            version = semantic.save_content(db, info.id, merged, source="refresh")
            _evolve_graph(db, info.id, merged, app_state=app_state)
            # 落了新版本 → 重建索引(I1: 版本与索引不漂移)
            try:
                from domains.chatbi import stores as cb_stores
                llm = runtime.get_llm(app_state) if app_state else None
                if llm is not None:
                    indexing.rebuild_index(
                        content=merged, data_source_id=info.id,
                        store=cb_stores.get_vector(app_state),
                        embedder=cb_stores.get_embedder(llm), db=db)
            except Exception as e:
                logger.warning("刷新后索引重建失败(降级, 手动重扫可修复): %s", e)
            results.append({"datasource_id": info.id, "version": version,
                            "models": len(merged.models), "ok": True})
        except Exception as e:
            logger.warning("自动刷新失败 %s: %s", info.name, e)
            results.append({"datasource_id": info.id, "ok": False, "error": str(e)[:200]})
    return {"refreshed": results}


def _merge_content(old, new):
    # old/new: SemanticModelContent(类型注解省略——避免模块顶层 import
    # semantic 造成的循环依赖, 运行时鸭子访问 .models/.sample_questions)
    """结构刷新合并(移植源 metadata_refresher._merge_content)。

    新扫描的表/列结构是权威(增删改), 但保留旧版本的 display_name/
    description/semantic_type/source/confidence/sample_questions——
    这些是人工/LLM 标注, 结构扫描(llm=None)只会产出退化值。
    """
    old_models = {m.name: m for m in old.models}
    for new_m in new.models:
        old_m = old_models.get(new_m.name)
        if old_m is None:
            continue  # 新表: 用扫描退化值, 等下次 LLM 富化
        new_m.display_name = old_m.display_name or new_m.display_name
        new_m.description = old_m.description or new_m.description
        # 指标/关系保留(I1): 刷新扫描(llm=None)只产规则 simple 指标 +
        # 外键/命名关系, 直接用会静默丢掉 LLM composite 指标(如 GMV)与
        # ai_inferred/implicit_mining 关系——语义层逐周期向裸结构退化。
        if old_m.metrics:
            # 旧指标全量保留: 结构刷新(llm=None)只会重产同名的规则 simple
            # 指标, 而 LLM composite(如 GMV)/人工校正指标只在旧版本里——
            # 丢了就是永久丢失(下次全量重扫也不一定复原)。
            new_m.metrics = list(old_m.metrics)
        if old_m.relationships:
            new_rels = list(new_m.relationships)
            new_rel_keys = {(r.name, r.target_model) for r in new_rels}
            for r in old_m.relationships:
                if (r.name, r.target_model) not in new_rel_keys:
                    new_rels.append(r)  # 旧多出的关系(ai_inferred 等)保留
            new_m.relationships = new_rels
        old_cols = {c.name: c for c in old_m.columns}
        for c in new_m.columns:
            old_c = old_cols.get(c.name)
            if old_c is None:
                continue  # 新列
            c.display_name = old_c.display_name or c.display_name
            c.description = old_c.description or c.description
            if old_c.semantic_type:
                c.semantic_type = old_c.semantic_type
            if old_c.source:
                c.source = old_c.source
            if old_c.confidence:
                c.confidence = old_c.confidence
    new.sample_questions = old.sample_questions or new.sample_questions
    return new


def _make_index_rebuilder(app_state, datasource_id: str):
    """构造图谱演化的索引重建回调(I2: 演化落新版本后索引不漂移)。

    apply_confidence_updates 以关键字调用 rebuild_index(content=..., data_source_id=...);
    向量设施不可用时返回 None(调用方按契约跳过重建, 只记 debug)。
    """
    try:
        from domains.chatbi import runtime, stores as cb_stores, indexing
        llm = runtime.get_llm(app_state) if app_state else None
        if llm is None:
            return None
        store = cb_stores.get_vector(app_state)
        embedder = cb_stores.get_embedder(llm)
        db = runtime.get_pack_db()
    except Exception as e:
        logger.warning("索引重建回调构造失败(演化后跳过重建): %s", e)
        return None

    def _rebuild(content, data_source_id, **_kw):
        return indexing.rebuild_index(
            content=content, data_source_id=data_source_id,
            store=store, embedder=embedder, db=db)
    return _rebuild


def _evolve_graph(db, datasource_id: str, content, app_state=None) -> None:
    """图谱置信度演化 (SEM-003;graph_infer 移植栈的接线点):
    查询历史(fewshot 示例 SQL) → 频繁 JOIN 表对挖掘 → confidence 提升 →
    乐观锁写回语义层新版本。

    app_state: 索引重建回调需要(向量 store/embedder 构造);None 时
    演化仍执行, 只是不重建索引。"""
    from domains.chatbi.graph_infer import (mine_implicit_relationships,
                                            apply_confidence_updates,
                                            sync_linkage_to_graph)
    try:
        from domains.chatbi import stores as cb_stores
        history = [row["sql"] for row in
                   cb_stores.list_fewshot_examples(db, datasource_id)
                   if row.get("sql")]
    except Exception as e:
        logger.warning("查询历史读取失败(跳过演化): %s", e)
        return
    existing = [r for m in content.models for r in m.relationships]

    # ── linkage 反哺先行: 不依赖 implicit mining 是否有产出 ──
    # 查询沉淀的 linkage 共现达到阈值就应 boost/建边;此前 sync 藏在
    # suggestions 非空的分支里——没有 SQL 挖掘建议时已积累的 linkage
    # 永远不同步(闭环断点, 复核报告 P0)。
    try:
        from domains.chatbi.memory import get_memory_store
        from domains.chatbi.graph_infer import sync_linkage_to_graph
        sync_res = sync_linkage_to_graph(
            db, get_memory_store(db), datasource_id,
            rebuild_index=_make_index_rebuilder(app_state, datasource_id))
        if sync_res:
            logger.info("linkage 图谱同步: %s (ds=%s)", sync_res, datasource_id)
    except Exception as e:
        logger.warning("linkage 图谱同步失败(降级): %s", e)

    suggestions = mine_implicit_relationships(history, existing)
    if not suggestions:
        return
    # 乐观锁: 先读当前 is_current 版本号, 传入 expected_version 防并发写冲突
    # (走查发现的差距——原系统有, pack 此前未启用)
    current_version = None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT version FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND is_current = 1", (datasource_id,)).fetchone()
            current_version = row["version"] if row else None
    except Exception as e:
        logger.warning("乐观锁版本读取失败(降级为不校验): %s", e)
    # C2 修复: 正确签名 (db, data_source_id, updates, new_pairs, expected_version)
    # — content 之前落到 updates 位、suggestions 落到 new_pairs 位(参数错位同 B2)
    updates = apply_confidence_updates(
        db, datasource_id, suggestions,
        expected_version=current_version,
        rebuild_index=_make_index_rebuilder(app_state, datasource_id))
    if updates:
        logger.info("图谱演化: %s (ds=%s)", updates, datasource_id)


def _connect_info(info) -> dict:
    kw = info.connection_kwargs()
    kw["db_type"] = info.db_type
    return kw


def _load_settings(app_state) -> dict:
    """读 pack 设置(设置页保存值 > env > schema 默认)。

    经 runtime.settings_reader(sdk.pack_api 门面)——不走 services 直连,
    且必须携带 settings_store:管理端热改的保存值要让后台任务即时生效。
    """
    try:
        from domains.chatbi import runtime
        return dict(runtime.settings_reader(app_state).all())
    except Exception:
        return {}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
