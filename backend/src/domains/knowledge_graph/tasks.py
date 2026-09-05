"""知识图谱插件的后台任务 handler(kg.import_document / kg.induce_schema)。

【导入流水线(kg.import_document)】
  解析 → 结构感知切块 → 向量准备(可选) → 逐批 LLM 抽取(批内并行,
  批间串行:更新已知实体词表 + 落 checkpoint) → 本体约束过滤 →
  Neo4j 幂等 MERGE + Milvus upsert → 统计回写

【防护机制】(全部可配,见 settings.schema.yaml"抽取与批次"组)
  - 单块重试 llm_max_retries;连续失败熔断 failure_threshold
  - 协作式取消(每批次检查点);文档被并发删除检测(每批次校验存在)
  - chunk 级 checkpoint(status=done 的块重跑跳过 → 断点续跑)
  - 幂等:重导先清理该 doc 旧数据(Neo4j 引用计数 + Milvus 按 doc 删)

【观测】
  - LLM 抽取/向量化调用 conv_id 记 task:{id} → 现有调用日志界面可追溯
  - 每批次任务日志(实体/关系数、耗时);进度百分比持久化

【已知实体词表(glossary)】
  批次间串行的核心收益:把已抽取实体 top-K 注入后续 prompt,约束跨块
  命名一致性(合并质量的根本保障)。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from domains.knowledge_graph import runtime
from sdk.doc_parser import chunk_text, parse_to_text
from sdk.graph_store import normalize_name
from sdk.pack_api import task_conv_id

logger = logging.getLogger(__name__)

# 进度权重:解析切块 2% / 向量准备 10% / 抽取批 88%
_W_PARSE, _W_VECTOR, _W_EXTRACT = 2, 10, 88

# 进行中的导入(doc_id -> task_id):防同一文档并发重复导入
_inflight: Dict[str, str] = {}
_inflight_lock = threading.Lock()

# register_tasks 注入的 app.state(取 llm_client / settings_store)
_app_state: Any = None


def register_tasks(manager, app_state=None) -> None:
    """装配钩子:注册任务类型(pack_manager 调用,带 app_state)。"""
    global _app_state
    _app_state = app_state
    manager.register("kg.import_document", run_import_document,
                     pack_name=runtime.PACK_NAME)
    manager.register("kg.induce_schema", run_induce_schema,
                     pack_name=runtime.PACK_NAME)
    # 终态兜底:_inflight 只靠 handler finally 释放会漏(pending 期被取消/
    # handler 被热切换清掉时 handler 根本不执行)。挂到任务管理器的终态
    # 回调上——任何路径到达终态都释放,防重表不再永久卡死该文档。
    manager.add_terminal_listener(_release_inflight_on_terminal)
    # 启动收敛:进程重启后遗留 importing 状态的文档,其任务已被标
    # interrupted,不会再有 handler 去收敛它——不处理就永远显示"导入中"。
    # 只在首次装配(=启动)时执行,且排除当前确实在跑/排队的导入任务,
    # 避免热切换误伤活任务。
    _recover_stale_importing(manager)


def _recover_stale_importing(manager) -> None:
    """把"没有存活任务支撑"的 importing 文档收敛为 failed(可重跑续传)。"""
    if not _app_state:
        return
    try:
        active_doc_ids = set()
        for status in ("pending", "running"):
            tasks, _ = manager.store.list_tasks(
                status=status, task_type="kg.import_document", limit=1000)
            for t in tasks:
                doc_id = str((t.get("payload") or {}).get("doc_id") or "")
                if doc_id:
                    active_doc_ids.add(doc_id)
        store = runtime.get_kg_store(_app_state)
        recovered = store.recover_importing_docs(active_doc_ids)
        if recovered:
            logger.info(f"启动收敛: {recovered} 个 importing 文档标记为 failed(任务已被中断)")
    except Exception:
        logger.exception("启动收敛 importing 文档失败(不影响服务启动)")


# ── 提交入口(api 调用) ───────────────────────────────────────

def submit_import(app_state, kb_id: str, doc_id: str, force: bool = False) -> Dict[str, Any]:
    """提交单文档导入任务(同库串行 queue_key;同文档并发去重)。

    Raises:
        ValueError: 文档不存在/不属于该库,或该文档已有进行中的导入。
    """
    store = runtime.get_kg_store(app_state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise ValueError("文档不存在")
    # 占位先入表再提交:占位与检查在同一临界区,双击/并发提交只有一个能过;
    # 若先提交后登记,任务可能已在另一线程跑完并 release(此时表还没登记,
    # release 落空)→ 我们再登记 = 永久泄漏。
    with _inflight_lock:
        if doc_id in _inflight:
            raise ValueError(f"该文档已有进行中的导入任务(任务 {_inflight[doc_id][:8]}…)")
        _inflight[doc_id] = "(提交中)"

    kb = store.get_kb(kb_id)
    try:
        task = app_state.task_manager.submit(
            "kg.import_document",
            payload={"kb_id": kb_id, "doc_id": doc_id, "force": bool(force)},
            title=f"导入文档: {doc['filename']} → {kb['name'] if kb else kb_id[:8]}",
            pack_name=runtime.PACK_NAME,
            queue_key=f"kg:{kb_id}",  # 同库串行:不并发写图
        )
    except Exception:
        _release_inflight(doc_id)
        raise
    with _inflight_lock:
        # 终态回调按 task id 精确释放;若极端时序下任务已瞬终态并按占位
        # 释放过(值不匹配未释放),这里登记后由下面再补一次释放判定
        _inflight[doc_id] = task["id"]
        if _task_is_terminal(app_state, task["id"]):
            _inflight.pop(doc_id, None)
    return task


def _task_is_terminal(app_state, task_id: str) -> bool:
    """轻查任务是否已到终态(极端快任务场景兜底,失败视为未终态)。"""
    try:
        t = app_state.task_manager.store.get_task(task_id)
        return bool(t and t.get("status") in ("succeeded", "failed", "cancelled", "interrupted"))
    except Exception:
        return False


def submit_induce_schema(app_state, kb_id: str, sample_chunks: int = 8) -> Dict[str, Any]:
    """提交本体归纳任务(抽样 chunk → LLM 归纳 → 存为待审提案)。"""
    store = runtime.get_kg_store(app_state)
    kb = store.get_kb(kb_id)
    if not kb:
        raise ValueError("知识库不存在")
    return app_state.task_manager.submit(
        "kg.induce_schema",
        payload={"kb_id": kb_id, "sample_chunks": max(2, min(int(sample_chunks or 8), 30))},
        title=f"归纳本体: {kb['name']}",
        pack_name=runtime.PACK_NAME,
        queue_key=f"kg:{kb_id}",
    )


def _release_inflight(doc_id: str) -> None:
    with _inflight_lock:
        _inflight.pop(doc_id, None)


def _release_inflight_on_terminal(task: Dict[str, Any]) -> None:
    """TaskManager 终态回调:kg.import_document 到终态即释放防重登记。

    按 task id 精确匹配(而不是盲目 pop doc_id)——防止旧任务的迟到终态
    误删新一次提交刚登记的防重项。
    """
    if task.get("taskType") != "kg.import_document":
        return
    payload = task.get("payload") or {}
    doc_id = str(payload.get("doc_id") or "")
    if not doc_id:
        return
    with _inflight_lock:
        if _inflight.get(doc_id) == task.get("id"):
            _inflight.pop(doc_id, None)


# ── 配置取值 ─────────────────────────────────────────────────

def _cfg(app_state, key: str, default):
    return runtime.settings_reader(app_state).get(key, default)


# ── 导入流水线 ───────────────────────────────────────────────

def run_import_document(handle) -> Dict[str, Any]:
    payload = handle.payload or {}
    kb_id = str(payload.get("kb_id") or "")
    doc_id = str(payload.get("doc_id") or "")
    force = bool(payload.get("force"))
    if not _app_state:
        raise RuntimeError("任务未正确注册(register_tasks 未注入 app_state)")

    app_state = _app_state
    store = runtime.get_kg_store(app_state)
    try:
        return _run_import(handle, app_state, store, kb_id, doc_id, force)
    except Exception as e:
        # 任务失败/取消:文档状态从 importing 收敛为 failed(可重跑续传),
        # 已完成块保留(下次免重跑)——不留悬空的 importing 态
        try:
            store.update_document(doc_id, import_status="failed", error=str(e)[:500])
        except Exception:
            logger.warning("标记文档导入失败时出错", exc_info=True)
        raise
    finally:
        _release_inflight(doc_id)


def _run_import(handle, app_state, store, kb_id: str, doc_id: str, force: bool) -> Dict[str, Any]:
    kb = store.get_kb(kb_id)
    doc = store.get_document(doc_id)
    if not kb or not doc or doc["kbId"] != kb_id:
        raise RuntimeError("知识库或文档不存在(可能已被删除)")

    # 幂等跳过:已成功且未强制重导
    if doc["importStatus"] == "succeeded" and not force:
        handle.set_progress(100, "内容未变化,已导入过,跳过")
        handle.log("文档此前已成功导入且未要求强制重导,直接跳过(幂等)")
        return {"skipped": True, "entities": doc["entityCount"],
                "relations": doc["relationCount"], "chunks": doc["chunkCount"]}

    conv_id = task_conv_id(handle.task_id)
    started = time.monotonic()
    handle.log(f"开始导入: {doc['filename']}({doc['sizeBytes']}B,库「{kb['name']}」)",
               filename=doc["filename"], size_bytes=doc["sizeBytes"],
               kb=kb["name"], force=force,
               previous_status=doc["importStatus"])
    store.update_document(doc_id, import_status="importing", error="")

    # 2) 解析 + 切块(结构感知)——先于清理:清理是否执行取决于是否存在
    #    可续跑的已完成块,必须先拿到 chunk 状态才能决定。
    #    【chunk 复用 = 断点续跑的前提】同一文档行的内容固定(内容变化会
    #    因 hash 查重生成新文档行),重跑时复用已有 chunk(保留 done 状态);
    #    force=True 才整体重建(全部重抽)。
    existing_chunks = store.list_chunks(doc_id)
    resume = bool(existing_chunks) and not force and any(
        c["status"] == "done" for c in existing_chunks
    )
    if existing_chunks and not force:
        chunks = existing_chunks
        done_n = sum(1 for c in chunks if c["status"] == "done")
        failed_n = sum(1 for c in chunks if c["status"] == "failed")
        handle.set_progress(_W_PARSE, f"复用已有切块: {len(chunks)} 块({done_n} 块已完成)")
        handle.log(f"复用已有 {len(chunks)} 块(断点续跑,已完成块将跳过)",
                   chunks=len(chunks), done=done_n, failed=failed_n,
                   pending=len(chunks) - done_n - failed_n)
    else:
        handle.set_progress(_W_PARSE, "解析文档")
        try:
            data = Path(doc["filePath"]).read_bytes() if doc["filePath"] else b""
            text = parse_to_text(doc["filename"], data)
        except FileNotFoundError:
            raise RuntimeError("原始文件已不存在,请重新上传")
        except ValueError as e:
            raise RuntimeError(f"文档解析失败: {e}")
        handle.log(f"文档解析: {len(data)}B → {len(text)} 字符({doc['filename'].rsplit('.', 1)[-1]} 格式)",
                   raw_bytes=len(data), text_chars=len(text))

        chunks = chunk_text(
            text,
            target_chars=_cfg(app_state, "chunk_target_chars", 1200),
            overlap_chars=_cfg(app_state, "chunk_overlap_chars", 100),
            max_chars=_cfg(app_state, "chunk_max_chars", 3000),
        )
        if not chunks:
            store.update_document(doc_id, import_status="failed", error="文档无有效文本")
            raise RuntimeError("文档解析后无有效文本(空文档/扫描件?)")
        store.replace_chunks(doc_id, kb_id, chunks)
        lens = [len(c["text"]) for c in chunks]
        handle.log(f"结构感知切块: {len(chunks)} 块(每块 {min(lens)}~{max(lens)} 字,平均 {sum(lens) // len(lens)})",
                   chunks=len(chunks), min_chars=min(lens), max_chars=max(lens),
                   avg_chars=sum(lens) // len(lens),
                   target=_cfg(app_state, "chunk_target_chars", 1200),
                   overlap=_cfg(app_state, "chunk_overlap_chars", 100))
        handle.set_progress(_W_PARSE, f"切块完成: {len(chunks)} 块")

    # 1) 清理旧贡献。
    #    只有"全量重导"(force / 首次导入 / 无任何已完成块的失败重跑)才清理;
    #    续跑路径(resume=True)严禁清理——已完成块的贡献只存在于 Neo4j/Milvus
    #    里,清了又不重抽(done 块被跳过)等于永久丢数据。
    graph = runtime.get_graph(app_state)   # 抽取批写入/收尾统计都要用,无条件获取
    if resume:
        handle.log("断点续跑: 保留图谱中已完成块的贡献,只补抽未完成块")
    else:
        try:
            removed = graph.delete_document(kb_id, doc_id)
            handle.log(f"清理旧图谱贡献: 删 {removed.get('edges', 0)} 边 / "
                       f"{removed.get('orphanEntities', 0)} 孤立实体(幂等重导)")
        except Exception as e:
            raise RuntimeError(f"Neo4j 清理失败: {e}")
        if kb.get("vectorEnabled"):
            try:
                runtime.get_vector(app_state).delete_by_doc(kb_id, doc_id)
            except Exception as e:
                handle.log(f"Milvus 旧向量清理失败(继续导入,完成后可重试): {e}", level="warn")

    # 3) 向量准备(模型未配置 → 纯图谱模式,不阻断)
    vector_ready = _prepare_vector(handle, app_state, store, kb, conv_id)

    # 4) 逐批抽取(批间串行:词表 + checkpoint;批内并行:LLM 调用)
    from engine.prompt_loader import PromptLoader
    packs_root = Path(__file__).resolve().parent.parent
    loader = PromptLoader(packs_root=packs_root)
    llm = app_state.llm_client

    batch_size = max(1, int(_cfg(app_state, "llm_batch_size", 4)))
    concurrency = max(1, int(_cfg(app_state, "llm_concurrency", 2)))
    max_retries = max(0, int(_cfg(app_state, "llm_max_retries", 2)))
    threshold = max(1, int(_cfg(app_state, "failure_threshold", 5)))
    glossary_top_k = max(0, int(_cfg(app_state, "glossary_top_k", 100)))
    temperature = int(_cfg(app_state, "extraction_temperature", 10)) / 100.0

    # 待处理 = pending + failed(失败块重跑时必须重抽;done 块跳过 = 断点)
    pending = [c for c in store.list_chunks(doc_id)
               if c["status"] in ("pending", "failed")]
    total = len(store.list_chunks(doc_id))
    done_before = total - len(pending)
    if done_before:
        handle.log(f"断点续跑: 跳过已完成的 {done_before} 块")
    handle.log(
        f"抽取配置: 批大小 {batch_size} / 批内并行 {concurrency} / 单块重试 {max_retries} "
        f"/ 熔断阈值 {threshold} / 词表 top-{glossary_top_k} / 温度 {temperature:.2f}"
        f" / 向量{'开' if vector_ready else '关'}",
        batch_size=batch_size, concurrency=concurrency, max_retries=max_retries,
        failure_threshold=threshold, glossary_top_k=glossary_top_k,
        temperature=temperature, vector=vector_ready, todo_chunks=len(pending),
        llm_model=llm.config.model if getattr(llm, "config", None) else "")

    glossary: Dict[str, str] = {}   # normalized_name -> type(本轮抽取累积)
    pending_proposals: List[Dict] = []
    total_entities = total_relations = failed_chunks = 0
    consecutive_failures = 0
    batch_index = 0

    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kg-extract")
    try:
        for batch_start in range(0, len(pending), batch_size):
            handle.check_cancel()
            _doc = store.get_document(doc_id)
            if not _doc:
                raise RuntimeError("文档在导入过程中被删除,任务中止")

            batch = pending[batch_start:batch_start + batch_size]
            batch_index += 1
            batch_started = time.monotonic()
            seqs = [c["seq"] for c in batch]
            handle.log(f"批次 {batch_index} 开始: 块 {seqs}(共 {len(batch)} 块)",
                       batch=batch_index, chunks=seqs)

            futures = {
                executor.submit(
                    _extract_chunk, loader, llm, kb, chunk, glossary, glossary_top_k,
                    temperature, max_retries, conv_id,
                ): chunk
                for chunk in batch
            }
            batch_entities: Dict[str, Dict] = {}
            batch_relations: List[Dict] = []
            batch_failed = 0
            failed_ids: set = set()
            for fut in futures:
                chunk = futures[fut]
                try:
                    entities, relations, stats = fut.result()
                except _ExtractionError as e:
                    stats = e.stats or {}
                    batch_failed += 1
                    failed_ids.add(chunk["id"])
                    store.mark_chunk(chunk["id"], "failed")
                    handle.log(
                        f"块 {chunk['seq']} 抽取失败(第{stats.get('attempt', '?')}次尝试,"
                        f"{stats.get('duration_ms', '?')}ms,prompt {stats.get('prompt_chars', '?')} 字): {e}",
                        level="warn",
                        chunk=chunk["seq"], chars=len(chunk["text"]),
                        attempt=stats.get("attempt"), duration_ms=stats.get("duration_ms"),
                        prompt_chars=stats.get("prompt_chars"),
                        glossary_size=stats.get("glossary_size"),
                        error=str(e)[:200])
                    continue
                except Exception as e:
                    # 非 LLM 调用本身的异常(如 prompt 模板渲染)——同样按
                    # 块级失败处理,保持"块失败→熔断→可续跑"语义,而不是
                    # 裸异常终止整个任务
                    batch_failed += 1
                    failed_ids.add(chunk["id"])
                    store.mark_chunk(chunk["id"], "failed")
                    handle.log(f"块 {chunk['seq']} 处理异常: {e}", level="warn",
                               chunk=chunk["seq"], chars=len(chunk["text"]),
                               error=str(e)[:200])
                    continue
                # 注意:这里不标 done——checkpoint 语义是"图谱已写入",
                # 标早了会在 upsert 失败时让续跑跳过该块(静默丢数据)
                # 请求级明细:prompt 规模/词表注入量/原始与归一化后条数
                # (raw vs 规范化后的差值 = 非法/重复/超限被丢弃的量)
                handle.log(
                    f"块 {chunk['seq']} 抽取完成: {len(entities)} 实体 / {len(relations)} 关系"
                    f"({len(chunk['text'])} 字,prompt {stats.get('prompt_chars', '?')} 字"
                    f",词表 {stats.get('glossary_size', 0)} 条,LLM {stats.get('duration_ms', '?')}ms)",
                    chunk=chunk['seq'], chars=len(chunk["text"]),
                    entities=len(entities), relations=len(relations),
                    prompt_chars=stats.get("prompt_chars"),
                    glossary_size=stats.get("glossary_size"),
                    duration_ms=stats.get("duration_ms"),
                    raw_entities=stats.get("raw_entities"),
                    raw_relations=stats.get("raw_relations"),
                    dropped_entities=max(0, (stats.get("raw_entities") or 0) - len(entities)),
                    dropped_relations=max(0, (stats.get("raw_relations") or 0) - len(relations)),
                    attempt=stats.get("attempt"))
                for ent in entities:
                    key = ent["normalized_name"]
                    if key in batch_entities:  # 批内合并(别名/描述合并)
                        batch_entities[key]["aliases"] = list(dict.fromkeys(
                            batch_entities[key]["aliases"] + ent.get("aliases", [])))
                        if not batch_entities[key].get("description"):
                            batch_entities[key]["description"] = ent.get("description", "")
                    else:
                        batch_entities[key] = ent
                batch_relations.extend(relations)

            if batch_failed:
                consecutive_failures += batch_failed
                failed_chunks += batch_failed
                if consecutive_failures >= threshold:
                    raise RuntimeError(
                        f"连续抽取失败 {consecutive_failures} 次(≥熔断阈值 {threshold}),"
                        f"任务中止——可修复后重跑(已完成块会跳过)")
            else:
                consecutive_failures = 0

            # 本体约束过滤(semi_open 的类型提案进待审)
            kept_e, kept_r, dropped, proposals = _enforce_schema(kb, list(batch_entities.values()), batch_relations)
            pending_proposals.extend(proposals)

            # 关系端点必须在已知实体集内(本批 ∪ 词表),悬空关系丢弃。
            # dropped 含实体+关系两类 strict 丢弃,悬空数不能拿它减——
            # 按端点重数一遍(strict 丢弃实体会连带其关系悬空,属正常)
            known = set(batch_entities.keys()) | set(glossary.keys())
            dangling = sum(1 for r in batch_relations
                           if r["source"] not in known or r["target"] not in known)
            kept_r = [r for r in kept_r if r["source"] in known and r["target"] in known]

            if kept_e or kept_r:
                t_graph = time.monotonic()
                graph.upsert_batch(kb_id, doc_id, kept_e, kept_r)
                graph_ms = int((time.monotonic() - t_graph) * 1000)
            else:
                graph_ms = 0
            # checkpoint 在图谱写入成功后落:done = "贡献已在图里",
            # upsert 抛异常时本批块保持原状态(pending/failed),续跑会重抽。
            # 抽取失败的块已在上面标 failed,这里只落成功的
            for c in batch:
                if c["id"] not in failed_ids:
                    store.mark_chunk(c["id"], "done")
            total_entities += len(kept_e)
            total_relations += len(kept_r)

            # 词表更新(top-K 截断在渲染时做,这里只累积)
            for ent in kept_e:
                glossary[ent["normalized_name"]] = ent.get("type") or ""

            # 向量补写(chunk 原文向量化,与抽取结果无关;失败只告警)
            vec_rows = 0
            if vector_ready:
                vec_rows = _vectorize_chunks(handle, app_state, store, kb, batch, conv_id)

            done_count = done_before + (batch_start + len(batch))
            pct = _W_PARSE + _W_VECTOR + int(_W_EXTRACT * done_count / max(1, total))
            handle.set_progress(min(99, pct), f"已处理 {done_count}/{total} 块")
            handle.log(
                f"批次 {batch_index} 完成: {len(batch)} 块 → {len(kept_e)} 实体 / {len(kept_r)} 关系"
                f"(累计 {total_entities}/{total_relations})"
                f"{'(丢弃 ' + str(dropped) + ' 条类型外数据)' if dropped else ''}"
                f",耗时 {time.monotonic() - batch_started:.1f}s",
                batch=batch_index, entities=len(kept_e), relations=len(kept_r),
                cumulative_entities=total_entities, cumulative_relations=total_relations,
                schema_dropped=dropped or 0, dangling_dropped=max(0, dangling),
                graph_ms=graph_ms, vector_rows=vec_rows,
                seconds=round(time.monotonic() - batch_started, 1))
    finally:
        executor.shutdown(wait=False)

    # 5) 类型提案合并进本体待审列表(semi_open)
    if pending_proposals:
        _merge_pending_proposals(store, kb, pending_proposals)
        handle.log(f"新增类型提案 {len(pending_proposals)} 项,待本体页审核", level="warn")

    # 6) 收尾统计
    final_status = "partial" if failed_chunks else "succeeded"
    doc_counts = graph.document_counts(kb_id, doc_id)
    entity_total, relation_total = doc_counts["entities"], doc_counts["relations"]
    store.update_document(
        doc_id, import_status=final_status,
        entity_count=entity_total, relation_count=relation_total,
        error="" if not failed_chunks else f"{failed_chunks} 块抽取失败(可重跑续传)",
    )
    duration = time.monotonic() - started
    handle.set_progress(100, "导入完成")
    handle.log(
        f"导入完成({final_status}): {entity_total} 实体 / {relation_total} 关系"
        f"/ {total} 块(其中 {failed_chunks} 块失败),总耗时 {duration:.1f}s"
        f"(平均 {duration / max(1, total) / 60:.1f} 分钟/块)",
        status=final_status, entities=entity_total, relations=relation_total,
        chunks=total, failed_chunks=failed_chunks, resumed_chunks=done_before,
        vector=vector_ready, seconds=round(duration, 1))
    return {
        "status": final_status, "chunks": total, "failedChunks": failed_chunks,
        "entities": entity_total, "relations": relation_total,
        "vectorEnabled": vector_ready, "durationSec": round(duration, 1),
    }


def _prepare_vector(handle, app_state, store, kb: Dict, conv_id: str) -> bool:
    """向量模式准备:已启用 → 校验模型一致性 + 确保 collection;未决 → 探测。

    Returns:
        True = 本库向量可用(后续按批 upsert);False = 纯图谱模式。
    """
    handle.set_progress(_W_PARSE + 1, "准备向量存储")
    try:
        if kb.get("vectorEnabled"):
            import os
            current_model = os.getenv("LLM_EMBED_MODEL", "").strip()
            stored_model = (kb.get("embeddingModel") or "").strip()
            # 模型一致性校验:换 embedding 模型 = 换维度,旧 collection 里
            # 的向量与新模型不可比。不校验的话后续 upsert/search 全部静默
            # 失败(每批 warn 一条),导入却仍报成功——向量检索"悄悄消失"。
            if stored_model and current_model and stored_model != current_model:
                handle.log(
                    f"embedding 模型已变更(库建立时 {stored_model},当前 {current_model}),"
                    f"向量不兼容——本库降级纯图谱模式。如需向量检索,请清空重建该库"
                    f"或在设置中切回原模型", level="warn",
                    stored_model=stored_model, current_model=current_model)
                store.set_kb_vector_info(kb["id"], stored_model, kb.get("vectorDim"), False)
                return False
            handle.log(f"向量模式: 沿用库配置 dim={kb.get('vectorDim')}(模型 {stored_model or '未记录'})",
                       dim=kb.get("vectorDim"), model=stored_model or "")
            runtime.get_vector(app_state).ensure_collection(kb["id"], kb["vectorDim"] or 1024)
            return True
        # 未决:探测 embedding 模型
        import os
        if not os.getenv("LLM_EMBED_MODEL", "").strip():
            handle.log("LLM_EMBED_MODEL 未配置,本库以纯图谱模式运行(检索无向量路)",
                       level="warn")
            store.set_kb_vector_info(kb["id"], "", None, False)
            return False
        probe = app_state.llm_client.embeddings(
            ["维度探测"], conv_id=conv_id, stage="kg.embed_probe")
        dim = len(probe[0])
        store.set_kb_vector_info(kb["id"], os.getenv("LLM_EMBED_MODEL"), dim, True)
        runtime.get_vector(app_state).ensure_collection(kb["id"], dim)
        handle.log(f"向量模式开启: 模型 {os.getenv('LLM_EMBED_MODEL')},dim={dim}",
                   model=os.getenv("LLM_EMBED_MODEL"), dim=dim)
        return True
    except Exception as e:
        handle.log(f"向量准备失败,降级纯图谱模式: {e}", level="warn")
        try:
            store.set_kb_vector_info(kb["id"], "", None, False)
        except Exception:
            pass
        return False


def _vectorize_chunks(handle, app_state, store, kb: Dict, chunks: List[Dict], conv_id: str) -> int:
    """把一批 chunk 向量化并 upsert(失败只告警,不阻断导入)。返回写入条数。"""
    try:
        embed_batch = max(1, int(_cfg(app_state, "embed_batch_size", 16)))
        vector_store = runtime.get_vector(app_state)
        n = 0
        for i in range(0, len(chunks), embed_batch):
            part = chunks[i:i + embed_batch]
            vectors = app_state.llm_client.embeddings(
                [c["text"] for c in part], conv_id=conv_id, stage="kg.embed")
            n += vector_store.upsert_chunks(kb["id"], [{
                "chunk_id": c["id"], "doc_id": c["docId"], "seq": c["seq"],
                "text": c["text"], "vector": vec,
            } for c, vec in zip(part, vectors)])
        return n
    except Exception as e:
        handle.log(f"向量写入失败(图谱不受影响): {e}", level="warn", rows=len(chunks))
        return 0


class _ExtractionError(RuntimeError):
    """块抽取失败(带最后一次 LLM 调用的请求级明细,供日志留痕)。"""
    def __init__(self, msg: str, stats: Optional[Dict] = None):
        super().__init__(msg)
        self.stats = stats or {}


def _extract_chunk(loader, llm, kb: Dict, chunk: Dict, glossary: Dict,
                   glossary_top_k: int, temperature: float,
                   max_retries: int, conv_id: str) -> Tuple[List[Dict], List[Dict], Dict]:
    """单块抽取(在批内工作线程执行):渲染 prompt → chat_json → 规范化。

    Returns:
        (entities, relations, stats) — 已归一化、已剔除自环与空名;
        stats 是本次 LLM 调用的请求级明细(prompt规模/耗时/原始条数/词表量)。
    Raises:
        重试耗尽后的最后一次异常。
    """
    schema = kb.get("schema") or {}
    semi_open = schema.get("schema_mode", "semi_open") != "strict"

    glossary_lines = []
    if glossary and glossary_top_k:
        # 高频优先(词表按插入序累积,取尾部 top-K 近似高频;稳定且无需计数)
        items = list(glossary.items())[-glossary_top_k:]
        glossary_lines = [f"- {name}({typ})" if typ else f"- {name}" for name, typ in items]

    # 围栏 defang:文档内容是不可信输入,原文里的 ``` 能闭合 extract.j2
    # 的代码围栏并注入抽取指令;统一换成无害的 ~~~ 再进 prompt
    safe_text = chunk["text"].replace("```", "~~~")

    prompt = loader.render(
        "knowledge_graph", "extract",
        entity_types=schema.get("entity_types") or [],
        relation_types=schema.get("relation_types") or [],
        glossary="\n".join(glossary_lines),
        chunk_text=safe_text,
        allow_new_types=semi_open,
    )

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            data = llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=temperature, conv_id=conv_id, stage="kg.extract",
            )
            entities, relations = _normalize_extraction(data)
            # 请求级留痕(对标 call_logs 的粒度):一次 LLM 调用一份明细,
            # 含输入输出规模与耗时——任务日志能逐行对上调用日志
            stats = {
                "attempt": attempt + 1, "prompt_chars": len(prompt),
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "raw_entities": len((data or {}).get("entities") or []) if isinstance(data, dict) else 0,
                "raw_relations": len((data or {}).get("relations") or []) if isinstance(data, dict) else 0,
                "glossary_size": len(glossary_lines),
                "error": "",
            }
            return entities, relations, stats
        except Exception as e:
            last_error = e
            failed_stats = {
                "attempt": attempt + 1, "prompt_chars": len(prompt),
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "raw_entities": 0, "raw_relations": 0,
                "glossary_size": len(glossary_lines),
                "error": str(e)[:200],
            }
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
    # 把最后一次失败的 stats 带出去(调用方记 warn 日志用)
    raise _ExtractionError(str(last_error), failed_stats) if last_error else RuntimeError("extract failed")


def _normalize_extraction(data: Dict) -> Tuple[List[Dict], List[Dict]]:
    """LLM 输出 → 规范化实体/关系(归一化锚点、剔空名/自环/非字符串)。

    条数上限是代码级强制(prompt 里的数量约束对 LLM 只是建议):恶意/
    跑飞的输出灌入海量伪实体会污染图谱,这里掐断在入口。
    """
    MAX_ENTITIES, MAX_RELATIONS = 60, 120
    raw_entities = data.get("entities") if isinstance(data, dict) else None
    raw_relations = data.get("relations") if isinstance(data, dict) else None

    entities: List[Dict] = []
    seen = set()
    for e in (raw_entities or []):
        if len(entities) >= MAX_ENTITIES:
            break
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name or len(name) > 120:
            continue
        normalized = normalize_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        entities.append({
            "name": name, "normalized_name": normalized,
            "type": str(e.get("type") or "concept").strip()[:60] or "concept",
            "description": str(e.get("description") or "").strip()[:500],
            "aliases": [str(a).strip()[:120] for a in (e.get("aliases") or [])
                        if str(a).strip()][:10],
            "chunk_id": "",
        })

    relations: List[Dict] = []
    for r in (raw_relations or []):
        if len(relations) >= MAX_RELATIONS:
            break
        if not isinstance(r, dict):
            continue
        source = normalize_name(str(r.get("source") or ""))
        target = normalize_name(str(r.get("target") or ""))
        rtype = str(r.get("type") or "").strip()[:60]
        if not source or not target or not rtype or source == target:
            continue
        relations.append({
            "source": source, "target": target, "type": rtype,
            "description": str(r.get("description") or "").strip()[:500],
            "evidence": str(r.get("evidence") or "").strip()[:500],
            "chunk_id": "",
        })
    return entities, relations


def _enforce_schema(kb: Dict, entities: List[Dict], relations: List[Dict]):
    """本体约束过滤:strict 丢弃类型外数据;semi_open 保留并生成待审提案。"""
    schema = kb.get("schema") or {}
    strict = schema.get("schema_mode", "semi_open") == "strict"
    entity_keys = {t.get("key") for t in (schema.get("entity_types") or []) if t.get("key")}
    relation_keys = {t.get("key") for t in (schema.get("relation_types") or []) if t.get("key")}

    kept_e, dropped, proposals = [], 0, []
    for e in entities:
        if e["type"] in entity_keys:
            e["type_status"] = "approved"
            kept_e.append(e)
        elif strict:
            dropped += 1
        else:
            e["type_status"] = "proposed"
            proposals.append({"kind": "entity", "key": e["type"], "label": e["type"]})
            kept_e.append(e)

    kept_r = []
    for r in relations:
        if r["type"] in relation_keys:
            kept_r.append(r)
        elif strict:
            dropped += 1
        else:
            proposals.append({"kind": "relation", "key": r["type"], "label": r["type"]})
            kept_r.append(r)

    return kept_e, kept_r, dropped, proposals


def _merge_pending_proposals(store, kb: Dict, proposals: List[Dict]) -> None:
    """把新提案并入 schema.pending_types(去重),原子写回。

    写前重读库的最新 schema——kb 是任务开始时的快照,长导入期间管理员
    可能已在本体页做过编辑/审批,拿旧快照整包写回会静默覆盖那些修改。
    """
    fresh = store.get_kb(kb["id"]) or kb
    schema = dict(fresh.get("schema") or {})
    pending = list(schema.get("pending_types") or [])
    existing = {(p.get("kind"), p.get("key")) for p in pending}
    for p in proposals:
        if (p["kind"], p["key"]) not in existing:
            existing.add((p["kind"], p["key"]))
            pending.append(p)
    schema["pending_types"] = pending
    store.update_kb(kb["id"], schema_json=schema)


# ── 本体归纳任务 ─────────────────────────────────────────────

def run_induce_schema(handle) -> Dict[str, Any]:
    payload = handle.payload or {}
    kb_id = str(payload.get("kb_id") or "")
    sample_target = int(payload.get("sample_chunks") or 8)
    if not _app_state:
        raise RuntimeError("任务未正确注册(register_tasks 未注入 app_state)")
    app_state = _app_state
    store = runtime.get_kg_store(app_state)
    kb = store.get_kb(kb_id)
    if not kb:
        raise RuntimeError("知识库不存在")

    conv_id = task_conv_id(handle.task_id)
    handle.set_progress(10, "收集文档样本")
    samples = _collect_samples(store, kb_id, sample_target, limit_chars=1500)
    if not samples:
        raise RuntimeError("库内没有可用文本(先上传并导入文档,或直接用模板本体)")
    handle.log(f"样本收集: {len(samples)} 段 / 共 {sum(len(s) for s in samples)} 字"
               f"(目标 {sample_target} 段,单段上限 1500 字)",
               samples=len(samples), total_chars=sum(len(s) for s in samples))

    handle.set_progress(40, f"LLM 归纳本体({len(samples)} 段样本)")
    from engine.prompt_loader import PromptLoader
    packs_root = Path(__file__).resolve().parent.parent
    loader = PromptLoader(packs_root=packs_root)
    prompt = loader.render("knowledge_graph", "induce_schema",
                           samples=samples, sample_count=len(samples))
    data = app_state.llm_client.chat_json(
        [{"role": "user", "content": prompt}],
        temperature=0.2, conv_id=conv_id, stage="kg.induce_schema",
    )
    if not isinstance(data, dict):
        # chat_json 的三级容错可能返回 list/str——与 _normalize_extraction
        # 同款防御,报人话错误而不是裸 AttributeError
        raise RuntimeError(f"归纳输出格式异常({type(data).__name__}),可重试")

    entity_types = _clean_induced(data.get("entity_types"), is_relation=False)
    relation_types = _clean_induced(data.get("relation_types"), is_relation=True)
    if not entity_types:
        raise RuntimeError("归纳结果为空(可重试或手写本体)")

    # 存为"整体本体提案"待审(不直接覆盖现本体)。写前重读:LLM 调用耗时
    # 分钟级,期间本体页的任何编辑不能被开始时的快照覆盖
    fresh = store.get_kb(kb_id) or kb
    schema = dict(fresh.get("schema") or {})
    schema["pending_schema_induction"] = {
        "entity_types": entity_types,
        "relation_types": relation_types,
        "induced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_count": len(samples),
    }
    store.update_kb(kb_id, schema_json=schema)
    handle.set_progress(100, "归纳完成,待审核")
    handle.log(f"本体归纳完成: {len(entity_types)} 实体类型 / "
               f"{len(relation_types)} 关系类型,已存为待审提案(本体页可一键应用)",
               entity_types=[t.get("key") for t in entity_types],
               relation_types=[r.get("key") for r in relation_types],
               samples=len(samples))
    return {"entityTypes": len(entity_types), "relationTypes": len(relation_types),
            "samples": len(samples)}


def _collect_samples(store, kb_id: str, target: int, limit_chars: int) -> List[str]:
    """抽样:优先已切块的中间 chunk(信息密度高),无 chunk 则现解析。"""
    samples: List[str] = []
    for doc in store.list_documents(kb_id):
        chunks = store.list_chunks(doc["id"], status="done") or store.list_chunks(doc["id"])
        if chunks:
            mid = len(chunks) // 2
            order = sorted(range(len(chunks)),
                           key=lambda i: abs(i - mid))  # 中间块优先(避开目录/结尾套话)
            for i in order:
                text = chunks[i]["text"].strip()
                if text:
                    samples.append(text[:limit_chars])
                if len(samples) >= target:
                    return samples
        elif doc["filePath"]:
            try:
                text = parse_to_text(doc["filename"], Path(doc["filePath"]).read_bytes())
                for c in chunk_text(text):
                    samples.append(c["text"].strip()[:limit_chars])
                    if len(samples) >= target:
                        return samples
            except Exception:
                continue
    return samples


def _clean_induced(items, is_relation: bool) -> List[Dict]:
    """清洗 LLM 归纳的类型列表(剔空/限长/字段规整)。"""
    cleaned = []
    if not isinstance(items, list):
        return cleaned
    for t in items[:16]:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key") or "").strip().lower().replace(" ", "_")[:40]
        label = str(t.get("label") or key).strip()[:30]
        if not key:
            continue
        entry = {
            "key": key, "label": label,
            "description": str(t.get("description") or "").strip()[:200],
            "examples": [str(e)[:50] for e in (t.get("examples") or [])][:3],
        }
        if is_relation:
            entry["domain"] = [str(d) for d in (t.get("domain") or [])][:6]
            entry["range"] = [str(r) for r in (t.get("range") or [])][:6]
        cleaned.append(entry)
    return cleaned
