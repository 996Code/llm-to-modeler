"""KG 标注回复(D32)—— 知识库的标准答案短路层。

【定位】知识库"老答错同一个问题"(典型:同名实体搞混)治本件:点赞
点踩只调概率下次可能还错;标注 = 「问题 → 标准答案」一对多库,
answer 前置短路——用户提问与标注问题做向量相似度,高阈值命中
(≈精确问法/强同义)直接返回标注答案,不走检索。错过的知识
"补课一次,终身正确"。

【固定问答(触发问法)】条目可配 triggers(多个触发问法):
归一化后与用户提问全等即命中(先于 embedding,零模型调用)——
"固定疑问触发固定答案"的确定路径。未命中仍走语义路(并集:
triggers 只增加命中面,不收窄语义 ≥0.90 的原有行为)。

【与相邻件的关系】
- 与 P2#14 反馈闭环:点踩记录是标注的候选队列(前端入口);本模块
  不读点踩数据,转化动作由管理端手动/接口做
- 与检索:短路未命中才走 hybrid_retrieve(标注是前置层不是替换层)

【表】kg_annotations(knowledge_graph schema):
  id / kb_id / question / answer / question_vec(vector)/ triggers(JSON数组)/
  enabled / created_at / updated_at。question_vec 建库时写入(bge-m3 定死口径,
  与 KG 检索向量同模型同库——复用 pgvector 设施,独立表)。
"""
import json
import logging
import re
import time
import unicodedata
import uuid
from typing import Any, Dict, List, Optional

from domains.knowledge_graph import runtime

logger = logging.getLogger(__name__)

# 短路阈值:cosine 相似度≥该值才直接返回标注答案。0.90 起步——
# 高阈值≈"精确问法或强同义改写",错伤面小;调低会拿标注硬套
# 相邻问题(答非所问比检索错误更伤信任)
MATCH_THRESHOLD = 0.90

# 触发问法上限:每条长度/条数——管理端手工维护的清单,超量说明
# 该走语义路或拆条,不给"一张网兜所有问法"的错觉
MAX_TRIGGERS = 20
MAX_TRIGGER_LEN = 200

# 归一化:空白全去(中文问句里的空格/换行与无空格写法对齐);
# 尾部疑问/感叹/句读/省略/波浪一律去("xxx?"/"xxx？"与"xxx"同问法)
_TRAILING_NOISE_RE = re.compile(r"[?？！!。．.…~～]+$")
_WS_RE = re.compile(r"\s+")


def _normalize_q(text: str) -> str:
    """触发问法归一化:NFKC(全角折半角/兼容形式)→ casefold →
    去全部空白 → 去尾部问句标点。两侧同口径,精确比较才稳。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip().casefold()
    s = _WS_RE.sub("", s)
    return _TRAILING_NOISE_RE.sub("", s).strip()


def _clean_triggers(triggers: Optional[List[str]], question: str) -> List[str]:
    """触发问法清洗:去空/超长校验/按归一化形式去重(与 question
    重复的自动剔除——question 本身永远参与精确匹配)。保序保留首次
    出现的原始拼写。"""
    question_key = _normalize_q(question)
    seen: set = {question_key} if question_key else set()
    clean: List[str] = []
    for raw in triggers or []:
        t = str(raw or "").strip()
        if not t:
            continue
        if len(t) > MAX_TRIGGER_LEN:
            raise ValueError(f"触发问法超过 {MAX_TRIGGER_LEN} 字: {t[:30]}…")
        key = _normalize_q(t)
        if not key or key in seen:
            continue
        seen.add(key)
        clean.append(t)
    if len(clean) > MAX_TRIGGERS:
        raise ValueError(f"触发问法最多 {MAX_TRIGGERS} 条(当前 {len(clean)} 条)")
    return clean


def _parse_triggers(raw: Any) -> List[str]:
    """DB triggers 列(JSON 文本)→ 字符串数组;坏值按空处理。"""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [t for t in data if isinstance(t, str)] if isinstance(data, list) else []


def _ensure_table(app_state, dim: int) -> None:
    """kg_annotations 建表(幂等;向量列维度跟随 kb 的 vector_dim——
    生产恒 1024(bge-m3 定死,建库预填),测试替身随 fake 库的 dim)。"""
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS kg_annotations (
                id TEXT PRIMARY KEY,
                kb_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                question_vec vector({dim}),
                triggers TEXT DEFAULT '[]',
                enabled INTEGER DEFAULT 1,
                source TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        # 存量库幂等补列(固定问答触发问法;JSON 数组文本,空=无触发问法)
        conn.execute(
            "ALTER TABLE kg_annotations ADD COLUMN IF NOT EXISTS triggers TEXT DEFAULT '[]'"
        )
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kg_annotations_kb
            ON kg_annotations(kb_id, enabled)""")


def _kb_dim(app_state, kb_id: str) -> int:
    """标注向量维度与库的检索向量同维(kb.vector_dim;缺省 bge-m3 1024)。"""
    kb = runtime.get_kg_store(app_state).get_kb(kb_id)
    dim = (kb or {}).get("vectorDim")
    return int(dim) if dim else 1024


def upsert_annotation(app_state, kb_id: str, question: str, answer: str,
                      annotation_id: Optional[str] = None,
                      source: str = "",
                      triggers: Optional[List[str]] = None) -> Dict[str, Any]:
    """新增/更新标注:问题向量化(bge-m3 定死口径)与写入同事务落库。

    triggers(触发问法):归一化后与用户提问全等即确定命中(零模型
    调用)。同库内精确键(question+triggers 的归一化集合)与其他条目
    冲突时抛 ValueError——精确命中必须唯一,否则"同一问法两条答案"
    的命中结果取决于遍历顺序,不可预期。
    """
    clean_triggers = _clean_triggers(triggers, question)
    vec = app_state.llm_client.embeddings(
        [question], conv_id=None, stage="kg.annotation_embed")[0]
    _ensure_table(app_state, _kb_dim(app_state, kb_id))
    store = runtime.get_kg_store(app_state)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    aid = annotation_id or str(uuid.uuid4())
    vec_lit = "[" + ",".join(str(x) for x in vec) + "]"
    triggers_lit = json.dumps(clean_triggers, ensure_ascii=False)
    with store._get_conn() as conn:
        _assert_no_conflict(conn, kb_id, aid, question, clean_triggers)
        conn.execute(
            """INSERT INTO kg_annotations
               (id, kb_id, question, answer, question_vec, triggers, enabled, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?::vector, ?, 1, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                 question = EXCLUDED.question, answer = EXCLUDED.answer,
                 question_vec = EXCLUDED.question_vec, triggers = EXCLUDED.triggers,
                 enabled = 1, source = EXCLUDED.source, updated_at = EXCLUDED.updated_at""",
            (aid, kb_id, question, answer, vec_lit, triggers_lit, source, now, now))
    return {"id": aid, "kbId": kb_id, "question": question,
            "answer": answer, "triggers": clean_triggers,
            "enabled": True, "source": source}


def _exact_keys(question: str, triggers: List[str]) -> set:
    """条目参与精确匹配的归一化键集合:{question} ∪ triggers。"""
    keys = {_normalize_q(question)}
    keys.update(_normalize_q(t) for t in triggers)
    return {k for k in keys if k}


def _assert_no_conflict(
        conn, kb_id: str, annotation_id: str, question: str,
        triggers: List[str]) -> None:
    """同库精确键冲突守卫:比对库内其他条目的 {question}∪triggers。"""
    new_keys = _exact_keys(question, triggers)
    if not new_keys:
        return
    rows = conn.execute(
        "SELECT id, question, triggers FROM kg_annotations WHERE kb_id = ?",
        (kb_id,)).fetchall()
    for row in rows:
        if row["id"] == annotation_id:
            continue
        clash = new_keys & _exact_keys(row["question"], _parse_triggers(row["triggers"]))
        if clash:
            raise ValueError(f"触发问法与现有条目冲突: {sorted(clash)[0]}")


def _table_exists(conn) -> bool:
    # 表按库惰性建(首用才建);未建过=集合为空,读路径不得跑 DDL
    # 或调模型(管理端 GET 空列表/检索短路都直接按"无标注"处理)。
    return conn.execute(
        "SELECT to_regclass('kg_annotations') IS NOT NULL AS present"
    ).fetchone()["present"]


def delete_annotation(app_state, kb_id: str, annotation_id: str) -> bool:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        if not _table_exists(conn):
            return False
        cur = conn.execute(
            "DELETE FROM kg_annotations WHERE id = ? AND kb_id = ?",
            (annotation_id, kb_id))
        return cur.rowcount > 0


def set_enabled(app_state, kb_id: str, annotation_id: str, enabled: bool) -> bool:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        if not _table_exists(conn):
            return False
        cur = conn.execute(
            "UPDATE kg_annotations SET enabled = ?, updated_at = ? "
            "WHERE id = ? AND kb_id = ?",
            (1 if enabled else 0, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             annotation_id, kb_id))
        return cur.rowcount > 0


def list_annotations(app_state, kb_id: str) -> List[Dict[str, Any]]:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            """SELECT id, kb_id, question, answer, triggers, enabled, source, created_at, updated_at
               FROM kg_annotations WHERE kb_id = ? ORDER BY created_at DESC""",
            (kb_id,)).fetchall()
    return [{
        "id": r["id"], "kbId": r["kb_id"], "question": r["question"],
        "answer": r["answer"], "triggers": _parse_triggers(r["triggers"]),
        "enabled": bool(r["enabled"]),
        "source": r["source"] or "", "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    } for r in rows]


def match_annotation(app_state, kb_id: str, query: str
                     ) -> Optional[Dict[str, Any]]:
    """标注短路:固定问答精确命中(零模型调用)→ 语义向量命中(≥阈值)。

    answer 前置层——kb_search/搜索 REST 在走检索前调用本函数;
    命中直接以标注作答(不走 hybrid_retrieve),并带 annotationHit
    标记供前端/观测区分"这是标准答案",matchType 区分
    exact(触发问法/固定问答)与 semantic(语义相似)。

    两段命中是并集:精确段先于 embedding——配置了触发问法的条目
    原样问出时零模型调用;未命中(含未配置 triggers 的库)走原
    语义路,所有 enabled 条目照常参与,行为不变。
    """
    store = runtime.get_kg_store(app_state)
    try:
        # ── 第一段:固定问答精确命中(先于一切模型调用) ──
        # 表按库惰性建:表不存在=无标注,直接 None(不跑 DDL 不调模型)
        nq = _normalize_q(query)
        if nq:
            with store._get_conn() as conn:
                if not _table_exists(conn):
                    return None
                rows = conn.execute(
                    """SELECT id, question, answer, triggers
                       FROM kg_annotations
                       WHERE kb_id = ? AND enabled = 1""",
                    (kb_id,)).fetchall()
            for row in rows:
                if nq in _exact_keys(row["question"], _parse_triggers(row["triggers"])):
                    return {
                        "id": row["id"], "question": row["question"],
                        "answer": row["answer"], "score": 1.0,
                        "matchType": "exact", "annotationHit": True,
                    }
        # ── 第二段:语义相似命中(原逻辑不变) ──
        vec = app_state.llm_client.embeddings(
            [query], conv_id=None, stage="kg.annotation_query",
            mode="query")[0]
        _ensure_table(app_state, len(vec))
        vec_lit = "[" + ",".join(str(x) for x in vec) + "]"
        with store._get_conn() as conn:
            row = conn.execute(
                """SELECT id, question, answer,
                          1 - (question_vec <=> ?::vector) AS score
                   FROM kg_annotations
                   WHERE kb_id = ? AND enabled = 1
                   ORDER BY question_vec <=> ?::vector
                   LIMIT 1""",
                (vec_lit, kb_id, vec_lit)).fetchone()
        if not row or (row["score"] or 0) < MATCH_THRESHOLD:
            return None
        return {
            "id": row["id"], "question": row["question"],
            "answer": row["answer"], "score": round(float(row["score"]), 4),
            "matchType": "semantic", "annotationHit": True,
        }
    except Exception as e:
        # 短路层故障不拖垮检索:记日志返回 None,回答走常规链路
        logger.warning(f"标注短路匹配失败(降级常规检索): {e}")
        return None
