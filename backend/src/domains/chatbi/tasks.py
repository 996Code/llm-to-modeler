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
    TaskHandle 只有 payload/进度上报, 平台组件(LLM 客户端)从装配期捕获。"""
    def _scan(handle):
        return _task_scan_datasource(handle, app_state)

    def _refresh(handle):
        return _task_refresh_semantics(handle, app_state)

    manager.register("chatbi.scan_datasource", _scan, pack_name=PACK_NAME)
    manager.register("chatbi.refresh_semantics", _refresh, pack_name=PACK_NAME)
    logger.info("chatbi tasks registered: scan_datasource / refresh_semantics")


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
        return {"models": len(content.models), "indexed": indexed,
                "datasource_id": ds_id}
    except Exception as e:
        datasources.update_datasource(db, ds_id, scan_status="failed",
                                      scan_error=str(e)[:500])
        raise


def _task_refresh_semantics(handle, app_state=None) -> dict:
    """自动刷新入口: 对全部 active 数据源重扫(内容指纹变化才落新版本,
    由 semantic.save_content 的版本幂等保证)。"""
    from domains.chatbi import runtime, semantic, datasources
    db = runtime.get_pack_db()
    llm = runtime.get_llm(app_state) if app_state else None
    settings = _load_settings(app_state)
    results = []
    for info in datasources.list_datasources(db, active_only=True):
        try:
            content = semantic.scan_datasource(
                llm=llm, db=db, connect_info=_connect_info(info),
                infer_metrics=bool(settings.get("scan_metric_inference", True)))
            _evolve_graph(db, info.id, content)
            results.append({"datasource_id": info.id, "models": len(content.models),
                            "ok": True})
        except Exception as e:
            logger.warning("自动刷新失败 %s: %s", info.name, e)
            results.append({"datasource_id": info.id, "ok": False, "error": str(e)[:200]})
    return {"refreshed": results}


def _evolve_graph(db, datasource_id: str, content) -> None:
    """图谱置信度演化 (SEM-003;graph_infer 移植栈的接线点):
    查询历史(fewshot 示例 SQL) → 频繁 JOIN 表对挖掘 → confidence 提升 →
    乐观锁写回语义层新版本。"""
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
    updates = apply_confidence_updates(db, datasource_id, content, suggestions,
                                       expected_version=current_version)
    if updates:
        # B2 修复: sync_linkage_to_graph 正确签名为 (db, mem_store, data_source_id)
        # ——此前 content 落到 mem_store 位, list_memories() 必炸 AttributeError
        from domains.chatbi.memory import get_memory_store
        sync_linkage_to_graph(db, get_memory_store(db), datasource_id)
        logger.info("图谱演化: %s (ds=%s)", updates, datasource_id)


def _connect_info(info) -> dict:
    kw = info.connection_kwargs()
    kw["db_type"] = info.db_type
    return kw


def _load_settings(app_state) -> dict:
    try:
        from services.pack_settings import PackSettingsReader
        from services.pack_settings import read_settings_schema
        schema = read_settings_schema("chatbi")
        from services.pack_settings import resolve_all
        return resolve_all("chatbi", schema=schema) if schema else {}
    except Exception:
        return {}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
