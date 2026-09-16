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
        # 健康巡检(health_check_interval_seconds, 缺省 5min, 对标源
        # APScheduler datasource_health 间隔)。周期每轮重读设置(管理端
        # 热改即时生效);检查粒度取 30s 轮询。
        _loop._last_refresh = _now_ts()   # 启动即视为已刷新(避免重启风暴)
        _loop._last_health = 0.0
        _loop._last_purge = 0.0   # 留存清理按小时级执行(四审 5.4: 每 30s 一次 DELETE 过频)
        while not _refresh_stop.wait(30):
            now = _now_ts()
            # 统计留存清理(每小时一次; 设置 retention 缺省 90 天, 0=关)
            if now - _loop._last_purge >= 3600:
                _loop._last_purge = now
                try:
                    retention = int(_load_settings(app_state)
                                    .get("query_stats_retention_days", 90))
                    if retention > 0:
                        from domains.chatbi.query_stats import purge_stats
                        from domains.chatbi.runtime import get_pack_db
                        purged = purge_stats(get_pack_db(), retention)
                        if purged:
                            logger.info("查询统计留存清理: 删除 %d 条(>%d天)", purged, retention)
                except Exception as e:
                    logger.warning("查询统计清理失败(下轮重试): %s", e)
            # 健康巡检: 设置周期(缺省 300s)
            try:
                health_iv = int(_load_settings(app_state)
                                .get("health_check_interval_seconds", 300))
            except Exception:
                health_iv = 300
            if health_iv > 0 and now - _loop._last_health >= health_iv:
                _loop._last_health = now
                try:
                    from domains.chatbi import datasources as ds_mod
                    from domains.chatbi.runtime import get_pack_db
                    ds_mod.check_all_health(
                        get_pack_db(),
                        max_failures=int(_load_settings(app_state)
                                         .get("health_check_max_failures", 3)))
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
    logger.info("chatbi scheduler started: health (from settings) + metadata refresh (from settings)")


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
        index_status = "ok"
        index_warning = None
        try:
            from domains.chatbi import indexing, stores
            store = stores.get_vector(app_state)
            embedder = stores.get_embedder(llm)
            rb = indexing.rebuild_index(content, ds_id, store, embedder, db=db)
            if rb is not None and getattr(rb, "error", None):
                raise RuntimeError(rb.error)   # RebuildResult.error 契约(五审5.2)
            indexed = rb.indexed_count
        except Exception as e:
            logger.warning("向量索引重建失败(降级, 不阻塞扫描): %s", e)
            index_status = "degraded"
            index_warning = f"扫描完成但索引重建失败——RAG 检索将降级, 重扫可修复: {str(e)[:120]}"
            handle.log(index_warning)
        datasources.update_datasource(
            db, ds_id, scan_status="done", scan_progress=100,
            scan_stage=f"完成: {len(content.models)} 张表, 索引 {indexed} 条",
            scanned_at=_now_iso())
        handle.log(f"扫描完成: {len(content.models)} 张表, 向量索引 {indexed} 条"
                   + (" (索引降级)" if index_status != "ok" else ""))
        return {"models": len(content.models), "indexed": indexed,
                "index_rebuild": index_status,
                **({"index_warning": index_warning} if index_warning else {}),
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

    # ── linkage → 图谱反哺: 目标 = 显式 ds ∪ 候选 scope ∪ linkage distinct ds ──
    # (三审 P0: 不能只取"成功产出合并记忆"的组——无产出/失败/仅 linkage
    #  时再次提交整理任务必须仍能触发同步, 重试语义才真实成立)
    graph_sync = []
    from domains.chatbi.memory import get_memory_store
    involved_ds = _graph_sync_targets(get_memory_store(db), result,
                                      data_source_id=data_source_id)
    for ds_id in sorted(involved_ds):
        try:
            from domains.chatbi.graph_infer import sync_linkage_to_graph
            res = sync_linkage_to_graph(
                db, get_memory_store(db), ds_id,
                rebuild_index=_make_index_rebuilder(app_state, ds_id),
                **_linkage_sync_kwargs(app_state))
            idx_state = (res or {}).get("index_rebuild", "ok")
            if str(idx_state).startswith("degraded"):
                handle.log(f"linkage 图谱同步完成 (ds={ds_id[:8]}…): "
                           f"版本已写, 但索引重建{idx_state}——RAG 检索可能滞后, 手动重扫可修复")
            else:
                handle.log(f"linkage 图谱同步完成 (ds={ds_id[:8]}…): {res}")
            graph_sync.append({"data_source_id": ds_id, "ok": True,
                               "index_rebuild": idx_state, "result": res})
        except Exception as e:
            logger.warning("整理后 linkage 图谱同步失败 (ds=%s): %s", ds_id, e)
            handle.log(f"linkage 图谱同步失败 (ds={ds_id[:8]}…, 降级): {e}")
            graph_sync.append({"data_source_id": ds_id, "ok": False, "error": str(e)[:200]})
    if not involved_ds:
        handle.log("无图谱同步目标(无候选记忆且无 linkage 记录)")
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
            # 6.6: save_content 幂等(内容不变返回旧版本)——结构无变化时
            # 用 current._version 判断是否真的写了新版本
            struct_changed = version != current.version if hasattr(current, 'version') else True
            evolve = _evolve_graph(db, info.id, merged, app_state=app_state)
            # 结构与图谱都无变化 → 跳过全量重建(7审 6.6: 无意义 embedding 压力)
            if not struct_changed and evolve.get("versions_written", 0) == 0:
                results.append({"datasource_id": info.id, "version": version,
                                "ok": True, "changed": False,
                                "detail": "结构无变化且无新图谱证据, 跳过重建"})
                continue
            # 六审 P1.C 修复: 演化可能写出 v+2/v+3——最终索引必须对齐
            # 演化后的 current, 不再用演化前的 merged 覆盖(旧关系内容)
            # 6.7: 同一次读取获得 content+version(防并发窗口 content 与
            # version 来自不同版本); load_content 返回 (content, version) 二元组
            from domains.chatbi import semantic as _sem
            _loaded = _sem.load_content(db, info.id)
            if _loaded and _loaded[0] is not None:
                final_content, final_version = _loaded
            else:
                final_content, final_version = merged, version
            index_status = "ok"
            index_warning = None
            try:
                from domains.chatbi import stores as cb_stores
                llm = runtime.get_llm(app_state) if app_state else None
                if llm is not None:
                    _rb = indexing.rebuild_index(
                        content=final_content, data_source_id=info.id,
                        store=cb_stores.get_vector(app_state),
                        embedder=cb_stores.get_embedder(llm), db=db)
                    if _rb is not None and getattr(_rb, "error", None):
                        raise RuntimeError(f"索引重建失败: {_rb.error}")
                else:
                    index_status = "skipped"
            except Exception as e:
                logger.warning("刷新后索引重建失败(降级, 手动重扫可修复): %s", e)
                index_status = "degraded"
                index_warning = f"语义已写 v{final_version}, 索引落后——手动重扫可修复"
            results.append({"datasource_id": info.id, "version": final_version,
                            "models": len(final_content.models), "ok": True,
                            "index_rebuild": index_status,
                            **({"index_warning": index_warning} if index_warning else {})})
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
        result = indexing.rebuild_index(
            content=content, data_source_id=data_source_id,
            store=store, embedder=embedder, db=db)
        # rebuild_index 的失败契约是返回 RebuildResult(error=...) 而非抛
        # 异常(五审 5.2)——error 非空时 raise, 让调用方的 except/
        # on_index_error 降级路径真实生效, 不再谎报成功
        if result is not None and getattr(result, "error", None):
            raise RuntimeError(f"索引重建失败: {result.error}")
        return result
    return _rebuild


def _linkage_sync_kwargs(app_state) -> dict:
    """linkage→图谱同步的阈值参数(设置页 > graph_core 默认常量)。

    复核报告 P2: 共现阈值/新表对阈值/置信度增量/发现开关原先硬编码
    常量, 不同数据规模只能改代码——统一收敛到设置链。
    """
    s = _load_settings(app_state)
    return {
        "co_occurrence_threshold": int(s.get(
            "graph_linkage_co_occurrence_threshold", 3)),
        "new_pair_threshold": int(s.get(
            "graph_linkage_new_pair_threshold", 5)),
        "confidence_boost": float(s.get(
            "graph_linkage_confidence_boost", 0.1)),
        "discover_new_pairs": bool(s.get(
            "graph_linkage_discover_new_pairs", True)),
    }


def _graph_sync_targets(store, result: dict, data_source_id=None) -> set:
    """计算图谱同步的目标数据源集合(三审 P0: 重试语义必须真实可用)。

    组成(并集):
      - 显式指定的 data_source_id —— 无论本次是否产生合并记忆都同步;
      - 本次参与整理的全部候选 scope(不只是成功产出组——首次失败/
        无产出时再次提交仍能命中同一批数据源);
      - 全局整理(未指定 ds)时: linkage 记忆的 distinct 数据源——
        linkage 永远不参与合并, 但它是图谱反哺的直接数据源,
        只有 linkage 也要同步(源: "数据源只有 linkage 记忆时提交
        整理仍会执行图谱同步"验收场景)。
    """
    targets: set = set()
    if data_source_id:
        targets.add(data_source_id)
    for s in result.get("candidate_scopes") or result.get("scopes") or []:
        if s.get("data_source_id"):
            targets.add(s["data_source_id"])
    if not data_source_id:
        # 全局整理: linkage 的 distinct ds 一并纳入(linkage 永不参与合并
        # 也永不被标记整理, 无需 consolidated 过滤)
        try:
            for m in store.list_memories():
                if m.get("type") == "linkage" and m.get("data_source_id"):
                    targets.add(m["data_source_id"])
        except Exception as e:
            logger.warning("linkage 数据源清单读取失败(跳过该来源): %s", e)
    return targets


def _evolve_graph(db, datasource_id: str, content, app_state=None) -> dict:
    """图谱置信度演化 (SEM-003;graph_infer 移植栈的接线点):
    查询历史(fewshot 示例 SQL) → 频繁 JOIN 表对挖掘 → confidence 提升 →
    乐观锁写回语义层新版本。

    六审 P1 重构: 两类信号(linkage + implicit)不再各自独立写版本——
    先聚合到同一 current 快照, 一次乐观锁写入; 索引重建由调用方在
    演化完成后统一执行(用最终 current, 不再用演化前的旧内容)。

    Returns:
        {"versions_written": int, "index_rebuild": str}
    """
    from domains.chatbi.graph_infer import (mine_implicit_relationships,
                                            apply_confidence_updates,
                                            sync_linkage_to_graph)
    from domains.chatbi import semantic as semantic_mod
    result = {"versions_written": 0, "index_rebuild": "skipped"}

    try:
        from domains.chatbi import stores as cb_stores
        history = [row["sql"] for row in
                   cb_stores.list_fewshot_examples(db, datasource_id)
                   if row.get("sql")]
    except Exception as e:
        logger.warning("查询历史读取失败(跳过演化): %s", e)
        return result

    # ── 聚合信号: linkage 共现 + implicit mining, 基于同一 current ──
    # 六审 P1.B: 此前 linkage 写完新版本后, implicit 仍用旧 existing
    # 计算——可能把刚提升的 confidence 写低。现在先取 linkage 的
    # 目标值, 再取 implicit 建议, 合并后一次写入。
    from domains.chatbi.memory import get_memory_store
    from domains.chatbi import semantic as semantic_mod
    from domains.chatbi.graph_infer import (linkage_memories_to_cooccurrence,
                                            _compute_confidence_updates,
                                            _discover_new_pairs,
                                            ensure_watermark_schema,
                                            set_watermark)
    kwargs = _linkage_sync_kwargs(app_state)
    cooccurrence = linkage_memories_to_cooccurrence(
        get_memory_store(db), datasource_id)

    # 重载 current(不依赖调用方传入的 content——可能已过时)
    current_content = semantic_mod.load_current_content(db, datasource_id)
    if current_content is None:
        return result
    existing = [r for m in current_content.models for r in m.relationships]

    all_updates: dict = {}
    all_new_pairs: list = []
    watermarks_to_set: list = []  # [(pair, signal, evidence)]

    try:
        ensure_watermark_schema(db)
    except Exception:
        pass  # 水位表建失败退化为无水位(全量计算, 不崩溃)

    # 信号 1: linkage 共现 → 水位感知 boost(七审 6.2:
    # 保留"共现达阈值即提升"的原语义 + 同证据不重复消费)
    if cooccurrence:
        link_updates, link_wm = _compute_confidence_updates(
            cooccurrence=cooccurrence,
            existing_relationships=existing,
            co_occurrence_threshold=kwargs["co_occurrence_threshold"],
            confidence_boost=kwargs["confidence_boost"],
            db=db, data_source_id=datasource_id, signal="linkage")
        all_updates.update(link_updates)
        watermarks_to_set.extend((p, "linkage", e) for p, e in link_wm.items())
        if kwargs["discover_new_pairs"]:
            all_new_pairs.extend(_discover_new_pairs(
                cooccurrence=cooccurrence,
                existing_relationships=existing,
                new_pair_threshold=kwargs["new_pair_threshold"]))

    # 信号 2: implicit mining(历史 SQL) → 水位感知(七审 6.3)
    # 七审 6.1 修复: 返回类型是 dict[pair, float], 用 .items() 遍历
    suggestions = mine_implicit_relationships(
        history, existing, db=db, data_source_id=datasource_id)
    for pair, conf in suggestions.items():
        if pair in all_updates:
            all_updates[pair] = max(all_updates[pair], conf)  # 取高不取低
        else:
            all_updates[pair] = conf
    # implicit 的水位也更新(证据量 = 达标历史的当前计数)
    from collections import Counter as _Counter
    import re as _re
    _table_re = _re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", _re.IGNORECASE)
    _known_names = set()
    for r in existing:
        f = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if f:
            _known_names.add(f)
            _known_names.add(r.target_model)
    _implicit_co = _Counter()
    for sql in history:
        _tabs = set(_table_re.findall(sql))
        for t1 in _known_names:
            for t2 in _known_names:
                if t1 < t2 and t1 in _tabs and t2 in _tabs:
                    _implicit_co[(t1, t2)] += 1
    for pair, cnt in _implicit_co.items():
        watermarks_to_set.append((pair, "implicit", cnt))

    if not all_updates and not all_new_pairs:
        # 无更新也推进水位(避免下轮重复计算同量证据)
        for pair, sig, ev in watermarks_to_set:
            try:
                set_watermark(db, datasource_id, pair, sig, ev)
            except Exception:
                pass
        return result  # 无新证据 → 不写版本(幂等)

    # 乐观锁: 读当前版本号
    current_version = None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT version FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND is_current = 1",
                (datasource_id,)).fetchone()
            current_version = row["version"] if row else None
    except Exception as e:
        logger.warning("乐观锁版本读取失败(降级为不校验): %s", e)

    # 不传 rebuild_index——索引由调用方在全部演化完成后统一重建(六审P1.C)
    from domains.chatbi.graph_infer import VersionConflictError
    try:
        apply_confidence_updates(
            db, datasource_id, all_updates,
            new_pairs=all_new_pairs or None,
            expected_version=current_version)
        result["versions_written"] = 1
        result["index_rebuild"] = "deferred"  # 调用方负责最终重建
        # 写入成功 → 推进水位(下次只算增量)
        for pair, sig, ev in watermarks_to_set:
            try:
                set_watermark(db, datasource_id, pair, sig, ev)
            except Exception:
                pass
        logger.info("图谱演化: %d updates, %d new_pairs (ds=%s)",
                    len(all_updates), len(all_new_pairs), datasource_id)
    except VersionConflictError as e:
        logger.warning("图谱演化版本冲突(有并发写入, 不覆盖): %s", e)
        result["index_rebuild"] = "conflict"
    except Exception as e:
        # 编程错误让任务失败可见(七审 6.4: 不再统称"可能版本冲突")
        logger.exception("图谱演化写入异常: %s", e)
        result["index_rebuild"] = "error"
        result["error"] = str(e)[:200]
    return result


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
