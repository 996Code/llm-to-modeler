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
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from domains.knowledge_graph import runtime
from sdk.doc_parser import chunk_text, parse_to_text, sub_chunks_for_embedding
from sdk.graph_store import normalize_name
from sdk.pack_api import task_conv_id

logger = logging.getLogger(__name__)

# 进度权重:解析切块 2% / 向量准备 10% / 抽取批 88%
_W_PARSE, _W_VECTOR, _W_EXTRACT = 2, 10, 88

# 在途导入查询:防重已下沉任务框架(submit 的 dedupe_key——占位/检查/
# 终态释放由框架统一保证,含 pending 期取消路径)。这里只保留只读视图
# 供删除守卫等场景使用。
def _fmt_eta(minutes: float) -> str:
    """预计剩余时间格式化:<1h 用分钟,否则小时+分钟。"""
    if minutes < 1:
        return "<1 分钟"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _inflight_task_id(app_state, doc_id: str):
    """该文档是否有进行中的导入任务(框架 dedupe 表只读查询)。"""
    mgr = getattr(app_state, "task_manager", None)
    return mgr.get_active_dedupe(f"kg.import:{doc_id}") if mgr else None

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
    # 防重占位/释放已下沉框架(dedupe_key),插件不再挂终态监听器。
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

def submit_import(app_state, kb_id: str, doc_id: str, force: bool = False,
                  chunk_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """提交单文档导入任务(同库串行 queue_key;同文档并发去重)。

    chunk_ids: 定向重抽——只补这些块(且仅其状态非 done;done 块的图谱
    贡献在库,重抽会破坏一致性,需换本体时走 force 全量)。定向路径绝不
    清理图谱(等同续跑语义)。

    Raises:
        ValueError: 文档不存在/不属于该库,或该文档已有进行中的导入。
    """
    store = runtime.get_kg_store(app_state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise ValueError("文档不存在")
    kb = store.get_kb(kb_id)
    # 同文档防重(dedupe_key)与同库串行(queue_key)都是框架语义:
    # 占位/检查同临界区、终态统一释放(含 pending 期取消),插件零样板。
    # 失败自动续跑(设置页 import_max_auto_retry):LLM 抖动/限流导致的失败
    # 由框架延迟重入队,块级 checkpoint 保证只跑剩余部分
    from sdk.pack_api import DuplicateTaskError
    max_retry = max(0, int(_cfg(app_state, "import_max_auto_retry", 3)))
    try:
        return app_state.task_manager.submit(
            "kg.import_document",
            payload={"kb_id": kb_id, "doc_id": doc_id, "force": bool(force),
                     "chunk_ids": list(chunk_ids or [])},
            title=f"导入文档: {doc['filename']} → {kb['name'] if kb else kb_id[:8]}",
            pack_name=runtime.PACK_NAME,
            queue_key=f"kg:{kb_id}",           # 同库串行:不并发写图
            dedupe_key=f"kg.import:{doc_id}",  # 同文档至多一个活任务
            max_auto_retry=max_retry,          # 失败自动续跑(致命错误除外)
        )
    except DuplicateTaskError as e:
        raise ValueError(str(e))  # api 层已有 ValueError→409 的映射


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


# ── 配置取值 ─────────────────────────────────────────────────

def _cfg(app_state, key: str, default):
    return runtime.settings_reader(app_state).get(key, default)


# ── 导入流水线 ───────────────────────────────────────────────

def run_import_document(handle) -> Dict[str, Any]:
    payload = handle.payload or {}
    kb_id = str(payload.get("kb_id") or "")
    doc_id = str(payload.get("doc_id") or "")
    force = bool(payload.get("force"))
    chunk_ids = [str(c) for c in (payload.get("chunk_ids") or []) if str(c).strip()]
    if not _app_state:
        raise RuntimeError("任务未正确注册(register_tasks 未注入 app_state)")

    app_state = _app_state
    store = runtime.get_kg_store(app_state)
    try:
        return _run_import(handle, app_state, store, kb_id, doc_id, force,
                           chunk_ids=chunk_ids or None)
    except Exception as e:
        # 任务失败/取消:文档状态从 importing 收敛为 failed(可重跑续传),
        # 已完成块保留(下次免重跑)——不留悬空的 importing 态
        try:
            store.update_document(doc_id, import_status="failed", error=str(e)[:500])
        except Exception:
            logger.warning("标记文档导入失败时出错", exc_info=True)
        raise


def _run_import(handle, app_state, store, kb_id: str, doc_id: str, force: bool,
                chunk_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    kb = store.get_kb(kb_id)
    doc = store.get_document(doc_id)
    if not kb or not doc or doc["kbId"] != kb_id:
        raise RuntimeError("知识库或文档不存在(可能已被删除)")
    targeted = [cid for cid in (chunk_ids or []) if str(cid).strip()]
    # 幂等跳过:已成功且未强制重导(定向重抽不受此限——它就是补漏动作)。
    # 向量缺口例外:done 块的向量当批写入失败只告警,不查缺口直接跳过的话
    # 缺口永久留存(重跑永远到不了补偿段)——有缺口则放行走补偿路径。
    if doc["importStatus"] == "succeeded" and not force and not targeted:
        holes = _missing_vector_chunks(app_state, store, kb, doc_id) \
            if kb.get("vectorEnabled") else []
        if not holes:
            handle.set_progress(100, "内容未变化,已导入过,跳过")
            handle.log("文档此前已成功导入且未要求强制重导,直接跳过(幂等)")
            return {"skipped": True, "entities": doc["entityCount"],
                    "relations": doc["relationCount"], "chunks": doc["chunkCount"]}
        handle.log(f"文档已导入,但存在 {len(holes)} 块向量缺口,进入补偿路径"
                   "(不重抽,只补向量)", level="warn", holes=len(holes))

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
    resume = (bool(existing_chunks) and not force and any(
        c["status"] == "done" for c in existing_chunks
    )) or bool(targeted)
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
            structural_max_chars=_cfg(app_state, "chunk_structural_max_chars", 10000),
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
    from sdk.prompt_loader import PromptLoader
    packs_root = Path(__file__).resolve().parent.parent
    loader = PromptLoader(packs_root=packs_root)
    llm = app_state.llm_client

    batch_size = max(1, int(_cfg(app_state, "llm_batch_size", 4)))
    concurrency = max(1, int(_cfg(app_state, "llm_concurrency", 2)))
    max_retries = max(0, int(_cfg(app_state, "llm_max_retries", 2)))
    threshold = max(1, int(_cfg(app_state, "failure_threshold", 5)))
    glossary_top_k = max(0, int(_cfg(app_state, "glossary_top_k", 100)))
    temperature = int(_cfg(app_state, "extraction_temperature", 10)) / 100.0
    # 抽取模型覆盖(空 = 全局模型):长文档导入换快模型的开关,
    # 只影响 kg.extract 调用,对话/检索链路不变
    extraction_model = str(_cfg(app_state, "extraction_model", "") or "").strip()

    # 待处理 = pending + failed(失败块重跑时必须重抽;done 块跳过 = 断点)
    pending = [c for c in store.list_chunks(doc_id)
               if c["status"] in ("pending", "failed")]
    total = len(store.list_chunks(doc_id))
    if targeted:
        # 定向重抽:只跑指定块里非 done 的(状态过滤见 submit_import 注释)。
        # 指定全为 done 时 pending 为空——记录后按空跑收尾,不动图谱。
        want = set(targeted)
        unknown = want - {c["id"] for c in store.list_chunks(doc_id)}
        if unknown:
            # 永久性校验错误:块 ID 写错重跑也恒定失败,标 Permanent 让
            # 框架跳过自动续跑(否则 5 分钟一轮无限空转,线上实证 3 小时)
            from services.task_manager import PermanentTaskError
            raise PermanentTaskError(f"定向重抽的块不存在: {sorted(unknown)[:3]}…")
        pending = [c for c in pending if c["id"] in want]
        handle.log(f"定向重抽: 指定 {len(want)} 块,待处理 {len(pending)} 块"
                   f"(其余为已完成,跳过)", targeted=len(want), to_run=len(pending))
    total = total if not targeted else len(pending)
    done_before = total - len(pending) if not targeted else 0
    if done_before:
        handle.log(f"断点续跑: 跳过已完成的 {done_before} 块")

    # ── 启动期向量对账:先还欠账,再跑新块 ──
    # 此前缺口补偿只在任务收尾跑:中途失败/取消的轮次,已 done 块的向量
    # 缺口要等"下一次完整跑完"才补——每轮都在累积欠账。挪到任务开头:
    # 每次启动先按向量库实存反查补齐历史缺口(幂等,无缺口时零成本),
    # 用户诉求"重启时先检查已处理的是否正确,不正确补全"。
    if vector_ready and done_before and not targeted:
        _backfill_vectors(handle, app_state, store, kb, doc_id, conv_id,
                          stage_label="启动期对账")
    _eff_model = extraction_model or (
        llm.config.model if getattr(llm, "config", None) else "")
    handle.log(
        f"抽取配置: 批大小 {batch_size} / 批内并行 {concurrency} / 单块重试 {max_retries} "
        f"/ 熔断阈值 {threshold} / 词表 top-{glossary_top_k} / 温度 {temperature:.2f}"
        f" / 向量{'开' if vector_ready else '关'} / 模型 {_eff_model}"
        + ("(覆盖)" if extraction_model else ""),
        batch_size=batch_size, concurrency=concurrency, max_retries=max_retries,
        failure_threshold=threshold, glossary_top_k=glossary_top_k,
        temperature=temperature, vector=vector_ready, todo_chunks=len(pending),
        llm_model=_eff_model, model_override=extraction_model or None)

    glossary: Dict[str, str] = {}   # normalized_name -> type(本轮抽取累积)
    # 关系类型约束表(key → {domain, range}),端点类型校验用(prompt 建议的代码强制)
    rel_specs = {r.get("key"): r for r in
                 ((kb.get("schema") or {}).get("relation_types") or []) if r.get("key")}
    # 图谱已存实体名缓存(懒加载一次):悬空关系过滤把已入库实体算进
    # known——续跑任务词表从零开始,不并入会把指向已存实体的关系全丢
    graph_entities_known: set = set()
    # 图谱已存实体别名表(normalized -> {type, aliases}),每批 upsert 后
    # 刷新:词表渲染时带出别名(张小凡 别名:鬼厉),引导 LLM 复用既有
    # 名称而不是另立变体——碎片的主要预防手段
    graph_alias_table: Dict[str, Dict[str, Any]] = {}
    # ── 词表播种(根修):续跑/定向重抽时词表从空开始,第一批 prompt
    # 完全不知道图里已有"林惊羽"——碎片的主要来源。把图内存量实体
    # (名+类型)作为词表初始值,重启后第一批就对齐既有命名。
    try:
        seeded = graph.list_entity_aliases(kb_id)
        for _name, _meta in seeded.items():
            glossary[_name] = _meta.get("type") or ""
        graph_alias_table = seeded
        if seeded:
            handle.log(f"词表播种: 从图谱载入 {len(seeded)} 个既有实体"
                       f"(续跑首批即可复用既有命名)", seeded=len(seeded))
    except Exception as e:
        logger.warning(f"词表播种失败(继续,词表从零累积): {e}")
    pending_proposals: List[Dict] = []
    total_entities = total_relations = failed_chunks = 0
    contested_total = 0  # 别名去争议累计(质量报告;常量大 = 抽取 prompt 该调了)
    consecutive_failures = 0
    # 预计剩余时间:最近 10 块的滑动平均耗时 × 剩余块 ÷ 并发
    # (LLM 耗时随词表膨胀/限流波动,滑动窗口比全程平均更贴近当前速率)
    _recent_durations: list = []
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
            # 块级"进行中"事件:块行从启动起就可见(加载态),完成时另发
            # 终态行——此前只有完成行,进行中的块在前端完全不可见
            for c in batch:
                handle.log(f"块 {c['seq']} 抽取中…", chunk=c["seq"],
                           chunk_state="started", chunk_id=c["id"])
            # 批内进度心跳:LLM 单块抽取分钟级,进度只在批次边界跳变的话
            # 用户侧感知"卡死"(E2E 实测 8 分钟停在 3%)。批开始即按
            # "已提交到 LLM"推进到本批起点,块完成再逐块推进
            base_done = done_before + batch_start
            handle.set_progress(
                min(99, _W_PARSE + _W_VECTOR + int(_W_EXTRACT * base_done / max(1, total))),
                f"批次 {batch_index}/{(len(pending) + batch_size - 1) // batch_size}: "
                f"LLM 抽取块 {seqs}…")

            futures = {
                executor.submit(
                    _extract_chunk, loader, llm, kb, chunk, glossary,
                    graph_alias_table, glossary_top_k,
                    temperature, max_retries, conv_id, extraction_model,
                ): chunk
                for chunk in batch
            }
            batch_entities: Dict[str, Dict] = {}
            batch_relations: List[Dict] = []
            batch_failed = 0
            failed_ids: set = set()
            completed_in_batch = 0
            for fut in futures:
                handle.check_cancel()  # 每个块处理前检查取消标志(防 LLM 慢时卡住整个批次)
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
                        chunk_state="failed", chunk_id=chunk["id"],
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
                               chunk_state="failed", chunk_id=chunk["id"],
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
                    chunk_state="done", chunk_id=chunk["id"],
                    entities=len(entities), relations=len(relations),
                    prompt_chars=stats.get("prompt_chars"),
                    glossary_size=stats.get("glossary_size"),
                    duration_ms=stats.get("duration_ms"),
                    raw_entities=stats.get("raw_entities"),
                    raw_relations=stats.get("raw_relations"),
                    dropped_entities=max(0, (stats.get("raw_entities") or 0) - len(entities)),
                    dropped_relations=max(0, (stats.get("raw_relations") or 0) - len(relations)),
                    attempt=stats.get("attempt"))
                # 块级溯源:实体/关系标注来源块 id(块产出查询/关系 MERGE
                # 键都依赖它;此前一直为空串,跨块关系被错误合并覆盖)。
                # 实体带 chunk_ids 列表:批内同名实体的多块溯源并集保留
                for ent in entities:
                    ent["chunk_id"] = chunk["id"]
                    ent["chunk_ids"] = [chunk["id"]]
                for r in relations:
                    r["chunk_id"] = chunk["id"]
                for ent in entities:
                    key = ent["normalized_name"]
                    if key in batch_entities:  # 批内合并(别名/描述/块溯源并集)
                        batch_entities[key]["aliases"] = list(dict.fromkeys(
                            batch_entities[key]["aliases"] + ent.get("aliases", [])))
                        if not batch_entities[key].get("description"):
                            batch_entities[key]["description"] = ent.get("description", "")
                        batch_entities[key]["chunk_ids"] = list(dict.fromkeys(
                            batch_entities[key].get("chunk_ids", []) + ent["chunk_ids"]))
                    else:
                        batch_entities[key] = ent
                batch_relations.extend(relations)
                # 块粒度进度心跳:批内每完成一块推进一次(批内并行,完成
                # 顺序不定——按"已完成块数"推,单调不回退)
                completed_in_batch += 1
                if stats.get("duration_ms"):
                    _recent_durations.append(float(stats["duration_ms"]))
                    del _recent_durations[:-10]  # 滑动窗口 10 块
                _eta = ""
                if len(_recent_durations) >= 2:
                    _avg = sum(_recent_durations) / len(_recent_durations)
                    _remain = total - (done_before + batch_start + completed_in_batch)
                    _eta_min = _avg * _remain / max(1, concurrency) / 60000.0
                    _eta = f", 预计剩余 {_fmt_eta(_eta_min)}" if _remain > 0 else ""
                handle.set_progress(
                    min(99, _W_PARSE + _W_VECTOR
                        + int(_W_EXTRACT * (base_done + completed_in_batch) / max(1, total))),
                    f"批次 {batch_index}: 已抽取 {completed_in_batch}/{len(batch)} 块{_eta}")

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

            # 别名独占性校验(泛称防火墙,入库前拦截):同一别名被互不
            # 互指的多个实体声称 = 泛称/歧义称谓,从本批所有声称者丢弃
            # ——LLM 建议代码验证的别名侧补全(名字有锚定过滤、关系有
            # 端点校验、类型有本体强制,aliases 此前是唯一裸奔字段)。
            contested = _drop_contested_aliases(kept_e, graph_alias_table)
            contested_total += contested
            if contested:
                handle.log(f"别名去争议: 丢弃 {contested} 个被多方声称的泛称别名",
                           level="warn", contested_aliases=contested)

            # 关系端点必须在已知实体集内(本批 ∪ 词表 ∪ 图谱已存),悬空关系丢弃。
            # 图谱已存实体必须并入:续跑/自动重跑任务的词表从零开始(块级
            # checkpoint 只存块状态),而前次运行抽的实体已在图里——只认
            # 本批∪词表会把"新块关系指向已入库实体"全部当悬空丢弃
            # (线上实测:续跑批次关系数为 0 的根因之一)。
            known = set(batch_entities.keys()) | set(glossary.keys())
            if done_before and not graph_entities_known:
                graph_entities_known = graph.list_entity_names(kb["id"])
            known |= graph_entities_known
            dangling = sum(1 for r in kept_r
                           if r["source"] not in known or r["target"] not in known)
            kept_r = [r for r in kept_r if r["source"] in known and r["target"] in known]

            # 端点类型约束(代码强制,prompt 里的 domain/range 只是建议):
            # 关系类型声明了 domain/range 时,两端实体类型必须落在约束内,
            # 违者丢弃并计数——典型违例如 师徒(person→organization)。
            # 端点类型查本批实体;词表实体只有 type 字符串,一并并入。
            # 未声明约束的关系类型(含 semi_open 新提案)不拦。
            type_of = {name: (e.get("type") or "") for name, e in batch_entities.items()}
            type_of.update({k: v for k, v in glossary.items() if v})

            def _endpoints_ok(r: Dict) -> bool:
                spec = rel_specs.get(r["type"])
                if not spec:
                    return True
                dom, rng = spec.get("domain") or [], spec.get("range") or []
                if dom and type_of.get(r["source"]) not in dom:
                    return False
                if rng and type_of.get(r["target"]) not in rng:
                    return False
                return True

            constraint_violating = sum(1 for r in kept_r if not _endpoints_ok(r))
            if constraint_violating:
                kept_r = [r for r in kept_r if _endpoints_ok(r)]

            if kept_e or kept_r:
                t_graph = time.monotonic()
                graph.upsert_batch(kb_id, doc_id, kept_e, kept_r)
                graph_ms = int((time.monotonic() - t_graph) * 1000)
            else:
                graph_ms = 0
            # 别名表刷新:本批 upsert 后图里多了新别名,供后续批次词表
            # 渲染(LLM 看到 张小凡 别名:鬼厉 → 复用既有命名,不另立变体)
            try:
                graph_alias_table = graph.list_entity_aliases(kb_id)
            except Exception as e:
                logger.warning(f"别名表刷新失败(词表退化为无别名): {e}")
            # checkpoint 在图谱写入成功后落:done = "贡献已在图里",
            # upsert 抛异常时本批块保持原状态(pending/failed),续跑会重抽。
            # 抽取失败的块已在上面标 failed,这里只落成功的
            for c in batch:
                if c["id"] not in failed_ids:
                    store.mark_chunk(c["id"], "done")
            total_entities += len(kept_e)
            total_relations += len(kept_r)

            # 词表更新(top-K 截断在渲染时做,这里只累积)。
            # pop+reinsert = 最近被抽到排尾(渲染取尾):普通 dict 赋值对
            # 已存在 key 不挪位,最早入库的主力角色会永远停在头部被截出
            # 词表——后续块丢失"复用既有名称"的锚点,同一人物分裂成多节点
            _glossary_touch(glossary, kept_e)

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

    # 5.5) 向量缺口补偿:done 块的向量在当批写入失败时只告警,而重跑会
    # 跳过 done 块——缺口因此永久留存(该块不参与向量召回)。这里按
    # Milvus 实存反向核对补齐,幂等(无缺口时一次 query 零成本返回)。
    if vector_ready:
        _backfill_vectors(handle, app_state, store, kb, doc_id, conv_id)

    # 5.6) 收尾图谱侧别名去争议(幂等):入库前校验只拦本批,历史遗留/
    # 旧版导入的泛称别名仍在图里——词表从图渲染,不清掉会持续回喂给
    # 后续抽取(回音室通道)。这里统一清掉,先于合并(碎片不该经由泛称并)。
    pruned_aliases = 0
    try:
        pruned_aliases = _prune_graph_aliases(graph, kb_id)
        if pruned_aliases:
            handle.log(f"收尾别名去争议: 清理 {pruned_aliases} 条图谱侧泛称别名",
                       level="warn", pruned=pruned_aliases)
    except Exception as e:
        handle.log(f"收尾别名去争议失败(不影响导入结论): {e}",
                   level="warn", error=str(e)[:200])

    # 碎片度量(质量报告):名字被其他实体声称为别名的节点数——收尾
    # 合并前后的差值即"安全网实际收敛了多少碎片"
    def _fragment_count() -> int:
        table = graph.list_entity_aliases(kb_id)
        names = set(table)
        return sum(1 for nm, info in table.items()
                   if any(normalize_name(a) in names and normalize_name(a) != nm
                          for a in (info.get("aliases") or [])))

    fragments_before = _fragment_count()

    # 5.7) 收尾消歧对账(幂等):即使 LLM 没在批次内对上碎片,全量扫描
    # "同类型 + 别名互指"的高置信对合并一次——把漏网的碎片(如 惊羽)
    # 收敛回 canonical,避免图谱残留碎片节点。
    merged_end = 0
    try:
        merged_end = _merge_same_type_alias_pairs(graph, kb_id, doc_id)
        if merged_end:
            handle.log(f"收尾消歧对账: 合并 {merged_end} 组同类型别名互指实体",
                       level="warn", merged=merged_end)
    except Exception as e:
        handle.log(f"收尾消歧对账失败(不影响导入结论): {e}",
                   level="warn", error=str(e)[:200])

    fragments_after = _fragment_count()

    # 5.8) 收尾描述连边(幂等):把 LLM 写进 description 却没建边的弱事实
    # 收回成兜底关系——孤立实体的主要来源。确定性子串匹配,零猜测;
    # 库没配兜底类型(缺省"相关")时整步跳过。
    linked = 0
    try:
        rel_keys = {t.get("key") for t in ((kb.get("schema") or {}).get("relation_types") or [])
                    if t.get("key")}
        if _FALLBACK_RELATION_KEY in rel_keys:
            linked = _backfill_mention_edges(graph, kb_id, doc_id)
            if linked:
                handle.log(f"收尾描述连边: 补 {linked} 条孤立实体兜底关系",
                           level="warn", linked=linked)
    except Exception as e:
        handle.log(f"收尾描述连边失败(不影响导入结论): {e}",
                   level="warn", error=str(e)[:200])

    # 5.9) 收尾 LLM 身份复合(可配):全局视野收最后一层碎片——块内没
    # 说出互指、对账收不了的残余(如 齐天大圣/孙悟空 各自成节点)。LLM
    # 只提语义候选,代码对每个提案在双方溯源块里找原文共现窗口,接地
    # 失败即拒绝。llm_identity_merge=false 可整段关闭。
    consolidation = {"proposals": 0, "applied": 0, "rejected": 0}
    if _cfg(app_state, "llm_identity_merge", True):
        try:
            consolidation = _run_consolidation(
                handle, loader, llm, graph, store, kb_id, doc_id,
                temperature=temperature, conv_id=conv_id,
                model_override=extraction_model or "")
            if consolidation.get("applied") or consolidation.get("rejected"):
                handle.log(
                    f"收尾身份复合: 提案 {consolidation['proposals']} 组,"
                    f"接地通过合并 {consolidation['applied']} 组,"
                    f"拒绝 {consolidation['rejected']} 组",
                    level="warn", **consolidation)
        except Exception as e:
            handle.log(f"收尾身份复合失败(不影响导入结论): {e}",
                       level="warn", error=str(e)[:200])

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
    # 质量报告:把"导入结果好不好"变成可比数字(实体身份子系统的
    # 度量闭环)—— contestedDrops 常量大 = 抽取 prompt 该调;
    # merged 高 = 抽取期消解没起作用; fragmentsAfter 残留 = 安全网
    # 也收不掉的碎片,人工处理清单。
    quality = {
        "contestedDrops": contested_total,
        "prunedAliases": pruned_aliases,
        "merged": merged_end,
        "fragmentsBefore": fragments_before,
        "fragmentsAfter": fragments_after,
        "mentionEdges": linked,
        "llmMergeProposals": consolidation.get("proposals", 0),
        "llmMergeApplied": consolidation.get("applied", 0),
        "llmMergeRejected": consolidation.get("rejected", 0),
    }
    handle.log(
        f"导入完成({final_status}): {entity_total} 实体 / {relation_total} 关系"
        f"/ {total} 块(其中 {failed_chunks} 块失败),总耗时 {duration:.1f}s"
        f"(平均 {duration / max(1, total) / 60:.1f} 分钟/块)",
        status=final_status, entities=entity_total, relations=relation_total,
        chunks=total, failed_chunks=failed_chunks, resumed_chunks=done_before,
        vector=vector_ready, seconds=round(duration, 1), **quality)
    return {
        "status": final_status, "chunks": total, "failedChunks": failed_chunks,
        "entities": entity_total, "relations": relation_total,
        "vectorEnabled": vector_ready, "durationSec": round(duration, 1),
        "quality": quality,
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
        embed_model = os.getenv("LLM_EMBED_MODEL", "").strip()
        backend = os.getenv("EMBEDDING_BACKEND", "local").strip().lower()
        if backend == "local":
            # local 后端不需要 LLM_EMBED_MODEL(进程内 onnx 推理),模型名固定
            embed_model = embed_model or "bge-m3"
        if not embed_model:
            handle.log("LLM_EMBED_MODEL 未配置,本库以纯图谱模式运行(检索无向量路)",
                       level="warn")
            store.set_kb_vector_info(kb["id"], "", None, False)
            return False
        probe = app_state.llm_client.embeddings(
            ["维度探测"], conv_id=conv_id, stage="kg.embed_probe")
        dim = len(probe[0])
        store.set_kb_vector_info(kb["id"], embed_model, dim, True)
        runtime.get_vector(app_state).ensure_collection(kb["id"], dim)
        handle.log(f"向量模式开启: 模型 {embed_model},dim={dim}",
                   model=embed_model, dim=dim)
        return True
    except Exception as e:
        handle.log(f"向量准备失败,降级纯图谱模式: {e}", level="warn")
        try:
            store.set_kb_vector_info(kb["id"], "", None, False)
        except Exception:
            pass
        return False


def _vectorize_chunks(handle, app_state, store, kb: Dict, chunks: List[Dict], conv_id: str) -> int:
    """把一批 chunk 向量化并 upsert(失败只告警,不阻断导入)。返回写入条数。

    子块粒度:抽取块可以是整章(万级字符),而 embedding 输入有限(本地
    bge-m3 实际 4096 token 截断,长块尾部对向量隐形 + 单向量长文语义稀释)
    ——按 vector_subchunk_chars(默认 1200)子切后逐段 embed,子块 id 挂
    父块(f"{chunk_id}#i")。抽取按章,召回按段。
    """
    try:
        # 默认 4 段/批(原 16):bge-m3 CPU 推理的 ONNX Arena 峰值与批大小
        # 线性相关,16 段×~1000tok 曾撞 4G cgroup 上限被 OOM kill(线上
        # 两次实锤)。选"降并发"而非"升内存":峰值约降至原 1/4,CPU 推理
        # 本就吃不满并发(2 线程 intra_op),段吞吐损失很小;内存上限只是
        # 保险丝,不是给浪费买单的
        embed_batch = max(1, int(_cfg(app_state, "embed_batch_size", 4)))
        sub_size = max(300, int(_cfg(app_state, "vector_subchunk_chars", 1200)))
        vector_store = runtime.get_vector(app_state)
        rows = []
        for c in chunks:
            for i, sub in enumerate(sub_chunks_for_embedding(c["text"], sub_size)):
                rows.append({
                    "chunk_id": f"{c['id']}#{i}", "doc_id": c["docId"],
                    "seq": c["seq"], "text": sub,
                })
        n = 0
        t_all = time.monotonic()
        for i in range(0, len(rows), embed_batch):
            part = rows[i:i + embed_batch]
            t0 = time.monotonic()
            vectors = app_state.llm_client.embeddings(
                [r["text"] for r in part], conv_id=conv_id, stage="kg.embed")
            n += vector_store.upsert_chunks(kb["id"], [
                {**r, "vector": vec} for r, vec in zip(part, vectors)])
            dt = time.monotonic() - t0
            # 向量化可观测:本地 CPU 推理分钟级是常态,无日志时表现为
            # "批次收尾长时间无输出"(线上被误判卡死/无法定位卡点)
            if dt > 3:
                handle.log(f"向量写入: {len(part)} 段 {dt:.1f}s(累计 {n}/{len(rows)})",
                           rows=len(part), seconds=round(dt, 1))
        if rows:
            handle.log(f"向量写入完成: {n} 段,耗时 {time.monotonic() - t_all:.1f}s",
                       rows=n, seconds=round(time.monotonic() - t_all, 1))
        return n
    except Exception as e:
        handle.log(f"向量写入失败(图谱不受影响): {e}", level="warn", rows=len(chunks))
        return 0


def _missing_vector_chunks(app_state, store, kb: Dict, doc_id: str) -> List[Dict]:
    """done 但向量库缺失的块(向量缺口补偿的统一判定)。

    以向量库实存为准,不依赖本地"已写"标记——幂等可重入;向量库不可达
    返回 [](无法判定时不动作,保持原有跳过/降级行为)。
    """
    try:
        vector_store = runtime.get_vector(app_state)
        # 向量按子块存(id = {chunk_id}#i):缺口判定以首个子块为准
        # (子块同批写入,首块在即视为整块已写;全缺则整块补)
        existing = vector_store.existing_chunk_ids(kb["id"], doc_id)
        missing = [c for c in store.list_chunks(doc_id, status="done")
                   if f"{c['id']}#0" not in existing]
        return missing
    except Exception:
        return []


def _backfill_vectors(handle, app_state, store, kb: Dict, doc_id: str,
                      conv_id: str, stage_label: str = "向量缺口补偿") -> int:
    """向量缺口补偿:图已入(done)但向量缺失的块补写(判定见 _missing_vector_chunks)。

    stage_label: 日志措辞——启动期对账(任务开头还欠账)或收尾补偿
    (当批写入失败的兜底)。
    """
    try:
        missing = _missing_vector_chunks(app_state, store, kb, doc_id)
        if not missing:
            return 0
        handle.log(f"{stage_label}: {len(missing)} 块已入图但缺向量,补写中",
                   level="warn", rows=len(missing))
        n = _vectorize_chunks(handle, app_state, store, kb, missing, conv_id)
        if n:
            handle.log(f"{stage_label}完成: 补写 {n} 段向量", rows=n)
        return n
    except Exception as e:
        handle.log(f"{stage_label}失败(图谱不受影响,下次重跑再补): {e}", level="warn")
        return 0


class _ExtractionError(RuntimeError):
    """块抽取失败(带最后一次 LLM 调用的请求级明细,供日志留痕)。"""
    def __init__(self, msg: str, stats: Optional[Dict] = None):
        super().__init__(msg)
        self.stats = stats or {}


def _extract_chunk(loader, llm, kb: Dict, chunk: Dict, glossary: Dict,
                   graph_aliases: Dict[str, Dict[str, Any]], glossary_top_k: int,
                   temperature: float,
                   max_retries: int, conv_id: str,
                   model_override: str = "") -> Tuple[List[Dict], List[Dict], Dict]:
    """单块抽取(在批内工作线程执行):渲染 prompt → chat_json → 规范化。

    model_override: 抽取模型覆盖(空 = 客户端配置默认)。长文档导入
    换快模型,不影响对话/检索链路。

    graph_aliases: 图谱已存实体的别名表(normalized -> [aliases])。词表
    渲染时把"同一实体的其他名字"显式列出来,引导 LLM 复用到 canonical
    名并把碎片名放进 aliases——实体消歧的代码级锚点。

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
        for name, typ in items:
            entry = graph_aliases.get(name) or {}
            aliases = entry.get("aliases") or []
            alias_note = f" 别名:{'/'.join(aliases[:8])}" if aliases else ""
            # 属性提示(描述截断):指称消解的特征对卡材料——"毛脸雷公嘴"
            # 之类描写靠它对到孙悟空,而不只靠名字匹配
            desc = (entry.get("description") or "").strip()
            attr_note = f" ·{desc[:24]}" if desc else ""
            glossary_lines.append(f"- {name}({typ}){alias_note}{attr_note}")

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
                model=model_override or None,
            )
            entities, relations = _normalize_extraction(data)
            # 原文锚定过滤(prompt 规则的代码强制):示例词幻觉(类型表
            # examples 被 LLM 照抄)、章节标题、整句引文都在这里掐掉,
            # 不让它们污染图谱。真实事故:通用模板示例"张三"被逐字抽进图
            entities, anchor_dropped = _anchor_filter(entities, chunk["text"])
            # 证据锚定(转述即清空):evidence 是审计轨迹,必须逐字来自
            # 原文——用 safe_text(LLM 实际看到的文本)比对
            evidence_blank = _blank_unanchored_evidence(relations, safe_text)
            # 请求级留痕(对标 call_logs 的粒度):一次 LLM 调用一份明细,
            # 含输入输出规模与耗时——任务日志能逐行对上调用日志
            stats = {
                "attempt": attempt + 1, "prompt_chars": len(prompt),
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "raw_entities": len((data or {}).get("entities") or []) if isinstance(data, dict) else 0,
                "raw_relations": len((data or {}).get("relations") or []) if isinstance(data, dict) else 0,
                "glossary_size": len(glossary_lines),
                "anchor_dropped": anchor_dropped,
                "evidence_blank": evidence_blank,
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


# ── 原文锚定过滤 ─────────────────────────────────────────────

# 章节标题/结构行:"第X章/回/节/卷/场/幕/篇"及常见首尾标记——是文档结构,
# 不是实体(真实事故:小说导入后"第三章 宏愿"成了图谱节点)
_HEADING_RE = re.compile(
    r"^(第\s*[0-9〇零一二两三四五六七八九十百千万]+\s*[章回节卷场幕篇部集]"
    r"|序章|序幕|序言|楔子|引子|尾声|终章|后记|跋|番外|附录|目录|正文)"
)
# 实体名不该含的标点:句读/分句符——含它基本是整句话被当成了实体
# (真实事故:"九天玄刹,化为神雷。煌煌天威,以剑引之"整句入图)
_NAME_PUNCT_RE = re.compile(r"[。,，!?！?;;\n\r…\"“”'‘’《》]")
# 超过这个长度的"name"几乎不可能是实体(30 字 ≈ 一整句)
_MAX_NAME_LEN = 30


def _anchor_filter(entities: List[Dict], chunk_text: str) -> Tuple[List[Dict], int]:
    """原文锚定过滤(post-LLM 代码强制,prompt 规则只是建议):

    1. 实体 name 必须逐字出现在 chunk 原文中——类型表 examples 被 LLM
       照抄(通用模板 person 示例"张三"逐字进图)、凭空幻觉都在这一层
       掐掉;
    2. 章节标题模式、含句读标点、超长的 name 直接丢弃——它们是文档结构
       或句子,不是实体。

    别名不参与锚定(线上实锤教训):LLM 会把 A 处的角色安上 B 处的名字
    (平顶山妖王被命名"黄袍怪"+别名"金角大王")——名字不在原文但别名
    在,旧版别名豁免让张冠李戴的实体入图,跨章名指认合并再把假身份
    焊死进图。别名是"身份断言"而非"出场事实",身份断言的校验在别处
    (独占性/互指佐证/枢纽防火墙),出场事实只认名字本身。代价是
    "本块仅以别名指称"的实体会被丢弃(宁漏不错):该实体的正式登场块
    (名字在文中)会重新建立它,词表引导后续块复用规范名。

    关系不在本层处理:关系端点可能指向词表里的跨块实体,批次层的
    known(本批 ∪ 词表)悬空过滤已覆盖;消解到词表规范名的关系端点
    不依赖本层(名字不必在本块原文)。被本层丢弃的实体不进批次实体集,
    引用它的关系会被悬空过滤连带剔除,行为一致。

    Returns:
        (保留的实体, 丢弃计数)
    """
    kept: List[Dict] = []
    dropped = 0
    for e in entities:
        name = str(e.get("name") or "").strip()
        if not name or _HEADING_RE.match(name) or _NAME_PUNCT_RE.search(name) \
                or len(name) > _MAX_NAME_LEN:
            dropped += 1
            continue
        if name in chunk_text:
            kept.append(e)
        else:
            dropped += 1
    return kept, dropped


def _glossary_touch(glossary: Dict[str, str], entities: List[Dict]) -> None:
    """把本批实体并入词表,最近被抽到的挪到尾部(渲染取尾 top-K)。

    词表语义是"给后续块的 LLM 提供复用锚点"——主力角色高频出现,pop+reinsert
    保证它们始终占据词表窗口;若用普通赋值,首批入库的名字固定在头部,大文档
    读到中段就被截出窗口,同一人物以别名另立节点(图碎裂的温床)。
    """
    for ent in entities:
        name = ent.get("normalized_name")
        if not name:
            continue
        glossary.pop(name, None)
        glossary[name] = ent.get("type") or ""


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


# 消歧对账保险阀:互指校验后的连通块仍超过该成员数,视为残余泛称粘连,
# 整块放弃合并(宁可漏并,不可错并;超限会在任务日志告警)
_MERGE_COMPONENT_MAX = 12


def _contested_alias_set(claims: Dict[str, Set[str]]) -> Set[str]:
    """别名争议判定(泛称识别)的核心:返回不能作身份锚点的别名集合。

    claims: 实体名 → 其声称的别名集合(归一化,不含自身名)。
    判定与合并守卫同语义:名字持有者是该名字的天然声称者;X 声称 Y 的
    名字 = X 自认同 Y(文本级指认),不算争议。一个别名只有在存在
    **两两互不指认**的声称者对时才是泛称("大王"被互不相识的精怪
    各自声称)——此时它不能唯一指认任何实体。
    """
    owners: Dict[str, Set[str]] = {}
    for nm, aliases in claims.items():
        for a in aliases:
            owners.setdefault(a, set()).add(nm)
        owners.setdefault(nm, set()).add(nm)

    def _identified(x: str, y: str) -> bool:
        return y in claims.get(x, ()) or x in claims.get(y, ())

    out: Set[str] = set()
    for a, o in owners.items():
        members = sorted(o)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not _identified(members[i], members[j]):
                    out.add(a)
                    break
            else:
                continue
            break
    return out


def _drop_contested_aliases(entities: List[Dict],
                            graph_aliases: Dict[str, Dict[str, Any]]) -> int:
    """别名独占性校验:被互不互指的多个实体共同声称的别名,从本批全部丢弃。

    别名的语义前提是唯一指认:"美猴王"只指孙悟空。而"大王/老妖"这类
    泛称会被 LLM 灌进多个互不相识实体的 aliases——进图后会在收尾对账
    被并查集当合并锚点,链式把整片角色粘成一坨(线上西游记 289 组误并
    事故的根因)。这是入库前的第一道拦截;图谱侧历史遗留由收尾的
    _prune_graph_aliases 兜底(词表从图渲染,不清掉会持续回喂)。

    entities 原地修改(aliases 字段收窄)。Returns: 丢弃的别名声称数。
    """
    claims: Dict[str, Set[str]] = {}
    for name, info in (graph_aliases or {}).items():
        claims[name] = {normalize_name(a) for a in (info.get("aliases") or []) if a}
    for e in entities:
        nm = e["normalized_name"]
        names = {normalize_name(a) for a in (e.get("aliases") or []) if a}
        names.discard(nm)
        claims[nm] = claims.get(nm, set()) | names
    for nm in claims:
        claims[nm].discard(nm)

    contested = _contested_alias_set(claims)
    if not contested:
        return 0

    dropped = 0
    for e in entities:
        nm = e["normalized_name"]
        keep = [raw for raw in (e.get("aliases") or [])
                if normalize_name(raw) == nm or normalize_name(raw) not in contested]
        dropped += len(e.get("aliases") or []) - len(keep)
        e["aliases"] = keep
    return dropped


def _prune_graph_aliases(graph, kb_id: str) -> int:
    """收尾图谱侧别名去争议(幂等):清掉库里残留的泛称别名,切断回音室。

    词表/别名表从图渲染并注入后续抽取 prompt——争议别名留在图里就会
    被反复"确认"。入库前校验只拦本批,历史遗留(旧版导入、合并吸收)
    在这里统一清理。争议判定复用 _contested_alias_set(与入库同语义)。
    移除的是"别名声称"而非实体本身;碎片合并信号(名字互指)不受影响
    (名字持有者与声称者之间永不构成争议对)。

    Returns: 移除的别名声称条数。
    """
    table = graph.list_entity_aliases(kb_id)
    if not table:
        return 0
    claims = {nm: {normalize_name(a) for a in (info.get("aliases") or []) if a}
              for nm, info in table.items()}
    for nm in claims:
        claims[nm].discard(nm)
    contested = _contested_alias_set(claims)
    if not contested:
        return 0
    # 移除名单用原始写法(Cypher 按字面匹配 aliases 列表)
    remove = sorted({a for info in table.values() for a in (info.get("aliases") or [])
                     if normalize_name(a) in contested})
    if not remove:
        return 0
    return graph.prune_aliases(kb_id, remove)


def _blank_unanchored_evidence(relations: List[Dict], chunk_text: str) -> int:
    """关系证据锚定:evidence 必须逐字来自块原文,转述/编造的清空。

    evidence 是消解与抽取的审计轨迹("让你确定的那句话")——转述会让
    "可回查"变成假审计。只清空不丢关系:事实本身仍成立(LLM 已按上下文
    判定),因转述而丢关系会误伤大量合法压缩写法。

    Returns: 被清空的条数。
    """
    n = 0
    for r in relations:
        ev = (r.get("evidence") or "").strip()
        if ev and ev not in chunk_text:
            r["evidence"] = ""
            n += 1
    return n


# ── 收尾 LLM 身份复合(5.9) ────────────────────────────────────
# LLM 只做语义提案("这两个名字像同一个对象"),代码做确定性接地:
# 提案对必须在双方溯源块的原文里找到 ≤span 字的共现窗口(两名独立
# 出现,短名不得只是长名的子串)才允许合并——幻觉需要伪造真实共现
# 才能落地。模拟实测(西游记 893 实体):9 提案过 4,拒绝均有据。
_CONSOLIDATE_BATCH = 120     # 每次名录调用的实体数
_CONSOLIDATE_SPAN = 80       # 证据共现窗口上限(无标点古文按滑窗不按句切)
_CONSOLIDATE_MAX_PROPOSALS = 20   # 每批提案上限(进 prompt,约束输出)
_LLM_MERGE_MAX = 50          # 单文档 LLM 复合合并的保险阀


def _names_independent_in(window: str, a: str, b: str) -> bool:
    """窗口内两名字是否"独立"出现:短名的某次出现不落在长名的出现内。

    防"虎先锋+先锋"式假共现——'先锋'只是'虎先锋'的子串时不算双名。
    """
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if short == long_:
        return False
    long_spans = [(m.start(), m.end()) for m in re.finditer(re.escape(long_), window)]
    for m in re.finditer(re.escape(short), window):
        if not any(ls <= m.start() and m.end() <= le for ls, le in long_spans):
            return True
    return False


def _find_pair_evidence(a: str, b: str, e_a: Dict, e_b: Dict,
                        chunk_texts: Dict[str, str],
                        span: int = _CONSOLIDATE_SPAN) -> str:
    """在两实体溯源块并集的原文里找双名共现窗口(逐字证据)。

    找不到返回空串 = 提案缺乏文本接地,拒绝合并。
    """
    seqs = set(e_a.get("source_chunks") or []) | set(e_b.get("source_chunks") or [])
    for cid in sorted(seqs):
        t = chunk_texts.get(cid) or ""
        if a not in t or b not in t:
            continue
        for ma in re.finditer(re.escape(a), t):
            for mb in re.finditer(re.escape(b), t):
                lo, hi = min(ma.start(), mb.start()), max(ma.end(), mb.end())
                if hi - lo <= span and _names_independent_in(t[lo:hi], a, b):
                    return t[lo:hi]
    return ""


def _run_consolidation(handle, loader, llm, graph, store, kb_id: str,
                       doc_id: str, temperature: float, conv_id: str,
                       model_override: str = "") -> Dict[str, int]:
    """收尾 LLM 身份复合:名录分批给 LLM 提案,代码接地验证后合并。

    与抽取/对账的分工:抽取只看单块(视角局部,齐天大圣/孙悟空 不一定
    在同块互指),对账只认已有的别名互指(没断言的收不了)——这里用
    全局视野补最后一层:LLM 看名录提语义候选,代码回查原文共现。

    Returns: {"proposals", "applied", "rejected"}(质量报告字段)。
    """
    stats = {"proposals": 0, "applied": 0, "rejected": 0}
    entities = graph.list_entities(kb_id)
    if len(entities) < 2:
        return stats
    by_name = {e["normalized"]: e for e in entities}
    chunk_texts = {c["id"]: (c.get("text") or "") for c in store.list_chunks(doc_id)}

    entities.sort(key=lambda e: (e.get("type") or "", e["normalized"]))
    proposals: List[Dict] = []
    seen_pairs = set()
    for i in range(0, len(entities), _CONSOLIDATE_BATCH):
        batch = entities[i:i + _CONSOLIDATE_BATCH]
        roster = "\n".join(
            f"- {e['name']}({e.get('type') or '?'})"
            f" {(e.get('description') or '')[:30]}"
            + (f" 别名:{'/'.join(e['aliases'][:8])}" if e.get("aliases") else "")
            for e in batch)
        prompt = loader.render(
            "knowledge_graph", "consolidate",
            roster=roster, entity_count=len(batch),
            max_proposals=_CONSOLIDATE_MAX_PROPOSALS)
        try:
            data = llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=temperature, conv_id=conv_id,
                stage="kg.consolidate", model=model_override or None)
        except Exception as e:
            handle.log(f"身份复合批次失败(跳过): {e}", level="warn",
                       error=str(e)[:160])
            continue
        for p in (data or {}).get("merges") or []:
            a = normalize_name(str((p or {}).get("a") or ""))
            b = normalize_name(str((p or {}).get("b") or ""))
            if a in by_name and b in by_name and a != b:
                key = (a, b) if a < b else (b, a)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    proposals.append(key)
        stats["proposals"] += len(seen_pairs)

    # 保险阀:提案过多视为名录语义失控,全部放弃(宁可漏并)
    if len(proposals) > _LLM_MERGE_MAX:
        handle.log(f"身份复合提案 {len(proposals)} 组超过保险阀 {_LLM_MERGE_MAX},"
                   f"本次全部放弃", level="warn")
        stats["rejected"] = len(proposals)
        return stats

    def _canon_key(name: str):
        e = by_name[name]
        return (e.get("created_at") or "9999",
                -len(e.get("source_chunks") or []),
                -len(name))

    for a, b in proposals:
        e_a, e_b = by_name[a], by_name[b]
        if (e_a.get("type") or "") != (e_b.get("type") or ""):
            stats["rejected"] += 1
            continue
        evidence = _find_pair_evidence(a, b, e_a, e_b, chunk_texts)
        if not evidence:
            stats["rejected"] += 1
            continue
        canon, frag = sorted((a, b), key=_canon_key)
        res = graph.merge_entities(kb_id, doc_id,
                                   [{"canonical": canon, "fragment": frag}])
        if int((res or {}).get("merged") or 0):
            stats["applied"] += 1
            handle.log(f"身份复合: {frag} → {canon}(证据:{evidence[:50]})",
                       level="warn", canonical=canon, fragment=frag)
        else:
            stats["rejected"] += 1

    # 复合合并把碎片别名并进 canonical,可能解锁新的别名互指——再收敛一遍
    try:
        _merge_same_type_alias_pairs(graph, kb_id, doc_id)
    except Exception:
        pass
    return stats


def _merge_same_type_alias_pairs(graph, kb_id: str, doc_id: str,
                                 max_passes: int = 5) -> int:
    """收尾消歧对账(不动点循环):反复跑合并单趟,直到一趟内无新合并。

    为什么必须循环:单趟的指认表在函数开头一次性建立,而合并会把碎片
    别名并进 canonical——A 并 B 后,A 新获得的别名可能恰好是另一个
    节点 C 的名字,这个新指认边在单趟建表时不存在。线上西游记实测:
    单趟结束后离线复跑还能再并 6 组(二魔→银角大王 等)。每趟严格
    减少节点数,必然终止;max_passes 是防御性上限。

    Returns: 各趟成功合并的总对数。
    """
    total = 0
    for _ in range(max(1, max_passes)):
        n = _merge_alias_pass(graph, kb_id, doc_id)
        total += n
        if not n:
            break
    return total


def _merge_alias_pass(graph, kb_id: str, doc_id: str) -> int:
    """收尾消歧对账单趟:按"文本揭示的别名指认"合并同类型重复实体。

    知识来源 = 正文:小说后段明确写的"张小凡后来叫鬼厉""万人往即鬼王"
    被 LLM 抽进 aliases(互指),这里按这条文本级指认把碎片并回本体。
    文本没说的,永不并——代码不做任何名字相似度类的猜测。

    合并判据(三条都满足才并):
      - 一方的名字出现在另一方的 aliases 里(指认边);
      - 双方类型一致(青云山 location / 青云门 organization 不碰);
      - 指认名通过互指佐证校验(见下)——泛称不作合并锚点。

    互指佐证(泛称防火墙,线上西游记实测教训):LLM 除了写真身份别名
    (张小凡→鬼厉),还会把泛称(妖怪/大王/老妖…)灌进几十个精怪的
    aliases——任何叫"大王"的实体都会被认领成锚点,并查集链式传导把
    猪八戒/沙僧/赛太岁/蜈蚣精 并成一坨(289 组合并的实锤事故)。因此
    一个名字只有在它的全部声称者两两互指时,才认定为"同一身份的独立
    佐证";声称者互不相识 = 泛称 = 不作锚点。
    指认可以成链(鬼王宗主→鬼王→万人往):用并查集把连通块整体收敛
    到一个节点,而不是单趟遍历(单趟会漏链尾)。canonical 选块内最早
    入库的名字(角色首次登场的名字,张小凡之于 鬼厉);其余名字全部
    保进 canonical 的 aliases——检索按别名匹配,不丢召回。

    保险阀:互指校验后的连通块若仍超大(> 成员数上限),视为残余粘连,
    整块放弃合并——宁可漏并,不可错并。

    Returns: 成功合并的对数。
    """
    try:
        entities = graph.list_entities(kb_id)
    except Exception:
        return 0
    if not entities:
        return 0
    by_name = {e["normalized"]: e for e in entities}

    # 别名→声称者 / 实体→其声称的别名(均归一化;互指校验与锚点判定共用)
    claimed: Dict[str, Set[str]] = {}
    claimants: Dict[str, Set[str]] = {}
    for e in entities:
        names = {normalize_name(a) for a in (e.get("aliases") or []) if a}
        names.discard(e["normalized"])
        claimed[e["normalized"]] = names
        for a in names:
            claimants.setdefault(a, set()).add(e["normalized"])

    anchor_ok_cache: Dict[str, bool] = {}

    def _anchor_ok(a: str) -> bool:
        """名字 a 的声称者们两两互指 → 同一身份的佐证;否则视为泛称。"""
        if a in anchor_ok_cache:
            return anchor_ok_cache[a]
        s = claimants.get(a) or set()
        ok = True
        members = sorted(s)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                x, y = members[i], members[j]
                if y not in claimed.get(x, ()) and x not in claimed.get(y, ()):
                    ok = False
                    break
            if not ok:
                break
        anchor_ok_cache[a] = ok
        return ok

    # ── 枢纽防火墙:桥接型称谓节点拒绝合并 ──
    # 通用判据(传递性连通,非两两比对):称谓节点 x 认领多个名字时,
    # 若"去掉 x 自己的指认边"后这些名字散落在 ≥2 个**有外部佐证**的
    # 连通分量里,x 就是横跨多个身份的桥——经由它的合并全部拒绝。
    #   外部佐证 = 分量里存在 x 之外的指认边(别的实体声称它/它声称
    #   别的实体)。只有 x 单向指认的哑叶子(如 老孙 仅被 孙悟空 指认)
    #   不构成独立分量——单边认领无害,误并损失只是一条别名。
    # 反例自检:合法大家族(孙悟空 声称 石猴/美猴王/齐天大圣/老孙)去掉
    # 孙悟空后,前三个经彼此的指认仍连成一片 → 单锚定分量 → 不是桥;
    # 事故场景(老妖 认领 黄风怪/黄袍怪)二者各成孤岛 → 双锚定分量
    # → 是桥,拒绝合并。不依赖任何名单,对任意领域通用。
    def _bridge_hubs() -> Set[str]:
        adj: Dict[str, Set[str]] = {}
        for nm2, al2 in claimed.items():
            for a2 in al2:
                adj.setdefault(nm2, set()).add(a2)
                adj.setdefault(a2, set()).add(nm2)

        hubs: Set[str] = set()
        for x, al in claimed.items():
            if len(al) < 2:
                continue
            parent: Dict[str, str] = {}

            def _root(v: str) -> str:
                parent.setdefault(v, v)
                while parent[v] != v:
                    parent[v] = parent[parent[v]]
                    v = parent[v]
                return v

            for nm2, al2 in claimed.items():
                if nm2 == x:
                    continue
                for a2 in al2:
                    ra, rb = _root(nm2), _root(a2)
                    if ra != rb:
                        parent[rb] = ra
            comps: Dict[str, List[str]] = {}
            for a in al:
                comps.setdefault(_root(a), []).append(a)
            if len(comps) < 2:
                continue

            def _anchored(members: List[str]) -> bool:
                return any(any(m != x for m in adj.get(c, ())) for c in members)

            if sum(1 for ms in comps.values() if _anchored(ms)) >= 2:
                hubs.add(x)
        return hubs

    hubs = _bridge_hubs()
    if hubs:
        logger.warning(f"收尾消歧对账: {len(hubs)} 个称谓枢纽拒绝合并"
                       f"(桥接多个互不连通的身份,如 {sorted(hubs)[:3]})")

    # ── 并查集:别名指认边(类型兼容 + 锚点可信 + 非枢纽才 union)──
    parent: Dict[str, str] = {n: n for n in by_name}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]   # 路径减半
            x = parent[x]
        return x

    def _types_ok(a: str, b: str) -> bool:
        ta = (by_name.get(a) or {}).get("type") or ""
        tb = (by_name.get(b) or {}).get("type") or ""
        return not (ta and tb and ta != tb)

    for e in entities:
        if e["normalized"] in hubs:
            continue
        for a in (e.get("aliases") or []):
            an = normalize_name(a)
            if (an in by_name and an != e["normalized"]
                    and _types_ok(e["normalized"], an)
                    and _anchor_ok(an)):
                ra, rb = _find(e["normalized"]), _find(an)
                if ra != rb:
                    parent[rb] = ra

    comps: Dict[str, List[str]] = {}
    for n in by_name:
        comps.setdefault(_find(n), []).append(n)

    def _canon_key(name: str):
        n = by_name[name]
        return (n.get("created_at") or "9999",
                -len(n.get("source_chunks") or []),
                -len(name))

    merged = 0
    for members in comps.values():
        if len(members) < 2:
            continue
        if len(members) > _MERGE_COMPONENT_MAX:
            logger.warning(
                f"收尾消歧对账: 连通块 {members[:5]}… 共 {len(members)} 成员,"
                f"超过上限 {_MERGE_COMPONENT_MAX},疑似泛称粘连,本次不合并")
            continue
        canon = min(members, key=_canon_key)
        for other in members:
            if other == canon:
                continue
            # 落库前再校验一次(canonical 可能吸收了异型节点,这里不跟)
            if not _types_ok(canon, other):
                continue
            res = graph.merge_entities(kb_id, doc_id,
                                       [{"canonical": canon, "fragment": other}])
            n = int((res or {}).get("merged") or 0)
            merged += n
            if n:
                by_name.pop(other, None)
    return merged


# 收尾描述连边的写入借用 upsert_batch 关系通道;MERGE 键含 chunk_id,用固定
# 哨兵值保证重跑幂等(同键 MERGE 合并),且不与任何真实块 id 相撞
_MENTION_CHUNK_ID = "desc-mention"
# 兜底关系类型的 key(模板约定);库没配该类型 = 该库不要弱边,整步跳过
_FALLBACK_RELATION_KEY = "相关"


def _backfill_mention_edges(graph, kb_id: str, doc_id: str) -> int:
    """收尾描述连边:实体 description 点名了别的库内实体 → 补一条兜底关系。

    与收尾消歧对账同哲学:知识来源 = 已落库文本,零猜测。抽取时 LLM 常把
    弱事实写进 description("送孙悟空去御马监到任的星官")却不为它建边
    (宁缺毋滥 + 关系类型不贴合就丢弃),实体因此孤立。这里把这些点名
    收回来——匹配是确定性的归一化子串命中,证据就是 description 原文,
    不经 LLM、不猜同义,跨块/跨批照样命中。

    连边规则(全部满足):
      - 被点名者名字(len>=2,canonical 名或别名)归一化后出现在点名者的
        description 里;
      - 两实体间当前没有任何边——已有关系不叠加弱边,防 hub 刷屏;
      - 长名优先:描述含"齐天大圣"时,不再按其子串别名"大圣"重复连。
    方向 = 点名者 → 被点名者;一对实体最多补一条。

    Returns: 新增关系条数。
    """
    entities = graph.list_entities(kb_id)
    if not entities:
        return 0
    connected = graph.list_connected_pairs(kb_id)

    # 名字索引:canonical 名 + 别名(均归一化)。len>=2 才参与——"孙"这类
    # 单字别名会在"孙悟空"等描述里大量误命中。
    name_owner: Dict[str, Dict] = {}
    for e in entities:
        cands = [e["normalized"]] + [normalize_name(a) for a in (e.get("aliases") or [])]
        for cand in cands:
            if len(cand) >= 2 and cand not in name_owner:
                name_owner[cand] = e

    # 长名优先:先命中"齐天大圣",其子串别名"大圣"同处命中即跳过
    cand_names = sorted(name_owner, key=len, reverse=True)

    new_rels: List[Dict] = []
    linked_pairs = set()
    for src in entities:
        desc_norm = normalize_name(src.get("description") or "")
        if not desc_norm:
            continue
        src_key = src["normalized"]
        hit_keys = set()
        for cand in cand_names:
            if cand not in desc_norm:
                continue
            owner = name_owner[cand]
            dst_key = owner["normalized"]
            # 自指,或已被本描述里更长的命中名覆盖(互为子串,如已命中
            # "齐天大圣"再遇其别名"大圣")则不重复连
            if dst_key == src_key or any(dst_key in k or k in dst_key
                                         for k in hit_keys):
                continue
            pair, rpair = (src_key, dst_key), (dst_key, src_key)
            if pair in linked_pairs or rpair in linked_pairs:
                continue
            if pair in connected or rpair in connected:
                continue
            hit_keys.add(dst_key)
            linked_pairs.add(pair)
            new_rels.append({
                "source": src_key, "target": dst_key,
                "type": _FALLBACK_RELATION_KEY,
                "description": f"描述点名:{owner['name']}",
                "evidence": (src.get("description") or "")[:60],
                "chunk_id": _MENTION_CHUNK_ID,
            })

    if not new_rels:
        return 0
    graph.upsert_batch(kb_id, doc_id, [], new_rels)
    return len(new_rels)


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

# 采样块的章节标题行占比上限:超过视为目录/结构页(标题行高度密集是
# 目录页的特征),不能代表正文的知识本体
_MAX_SAMPLE_HEADING_DENSITY = 0.3
# 采样块至少需要的非标题行数:纯几行标题 + 零星文字的块没有归纳价值
_MIN_SAMPLE_CONTENT_LINES = 3
_HEADING_LINE_RE = re.compile(
    r"^\s*(第\s*[0-9〇零一二两三四五六七八九十百千万]+\s*[章回节卷场幕篇部集]"
    r"|序章|序幕|序言|楔子|引子|尾声|终章|后记|跋|番外|附录)"
)


def _heading_density(text: str) -> float:
    """章节标题行占比(0~1)——目录页的特征信号。空文本视为全标题。"""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 1.0
    return sum(1 for l in lines if _HEADING_LINE_RE.match(l)) / len(lines)


def _pick_samples(texts: List[str], target: int, limit_chars: int) -> List[str]:
    """从候选块中挑选归纳样本(纯函数,便于测试)。

    - 中间块优先(目录在头部、结尾常是套话),从中间向两头扫描;
    - 目录密度过滤:标题行占比超限或正文行数不足的块跳过;
    - 全部被过滤时退化为密度最低的 target 块——带目录噪声归纳好过硬
      失败,调用方会看到 sample_count 与提示。
    """
    scored = []
    for idx, t in enumerate(texts):
        head = t[:limit_chars]
        density = _heading_density(head)
        lines = [l for l in head.splitlines() if l.strip()]
        scored.append((density, len(lines) >= _MIN_SAMPLE_CONTENT_LINES, idx, t))
    mid = len(scored) // 2
    order = sorted(range(len(scored)), key=lambda i: abs(i - mid))

    samples: List[str] = []
    fallback: List[Tuple[float, int, str]] = []
    for i in order:
        if len(samples) >= target:
            break
        density, rich, _idx, t = scored[i]
        if rich and density <= _MAX_SAMPLE_HEADING_DENSITY:
            samples.append(t[:limit_chars])
        else:
            fallback.append((density, _idx, t))
    if len(samples) < target and fallback:
        fallback.sort(key=lambda x: (x[0], x[1]))
        samples.extend(t[:limit_chars] for _d, _i, t in fallback[:target - len(samples)])
    return samples[:target]


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
    from sdk.prompt_loader import PromptLoader
    packs_root = Path(__file__).resolve().parent.parent
    loader = PromptLoader(packs_root=packs_root)
    prompt = loader.render("knowledge_graph", "induce_schema",
                           samples=samples, sample_count=len(samples))
    # 归纳与抽取同为批量离线链路,共用模型覆盖设置
    model_override = str(_cfg(app_state, "extraction_model", "") or "").strip()
    data = app_state.llm_client.chat_json(
        [{"role": "user", "content": prompt}],
        temperature=0.2, conv_id=conv_id, stage="kg.induce_schema",
        model=model_override or None,
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
    """抽样:与抽取同视角——优先已切块的 chunk,无 chunk 则用同一条切块器
    现切全书,再跨全书均匀取样(中间优先,不从文件头顺取)。

    两条路径都过"目录密度"过滤:标题行占比过高的块是目录/结构页,只见
    目录的归纳会产出 book/chapter 结构本体而非业务实体(真实事故:诛仙
    全书开头是目录页,归纳结果全是书籍结构)。全部候选被过滤时退化为
    取密度最低的块——带噪声归纳好过硬失败,提示语会说明样本质量。
    """
    texts: List[str] = []
    for doc in store.list_documents(kb_id):
        chunks = store.list_chunks(doc["id"], status="done") or store.list_chunks(doc["id"])
        if not chunks and doc["filePath"]:
            try:
                text = parse_to_text(doc["filename"], Path(doc["filePath"]).read_bytes())
                chunks = chunk_text(text)  # 与导入同一切块器,采样/抽取同粒度
            except Exception:
                continue
        for c in chunks or []:
            t = (c.get("text") if isinstance(c, dict) else getattr(c, "text", "")) or ""
            if t.strip():
                texts.append(t.strip())
            if len(texts) >= target * 8:  # 候选池上限,防超大文档全量驻内存
                break
        if len(texts) >= target * 8:
            break
    return _pick_samples(texts, target, limit_chars)


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
