"""KG 标注回复(D32)—— 知识库的标准答案短路层。

【定位】知识库"老答错同一个问题"(典型:同名实体搞混)治本件:点赞
点踩只调概率下次可能还错;标注 = 「问题 → 标准答案」一对多库,
answer 前置短路——用户提问与标注问题做向量相似度,高阈值命中
(≈精确问法/强同义)直接返回标注答案,不走检索。错过的知识
"补课一次,终身正确"。

【与相邻件的关系】
- 与 P2#14 反馈闭环:点踩记录是标注的候选队列(前端入口);本模块
  不读点踩数据,转化动作由管理端手动/接口做
- 与检索:短路未命中才走 hybrid_retrieve(标注是前置层不是替换层)

【表】kg_annotations(knowledge_graph schema):
  id / kb_id / question / answer / question_vec(vector)/ enabled /
  created_at / updated_at。question_vec 建库时写入(bge-m3 定死口径,
  与 KG 检索向量同模型同库——复用 pgvector 设施,独立表)。
"""
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from domains.knowledge_graph import runtime

logger = logging.getLogger(__name__)

# 短路阈值:cosine 相似度≥该值才直接返回标注答案。0.90 起步——
# 高阈值≈"精确问法或强同义改写",错伤面小;调低会拿标注硬套
# 相邻问题(答非所问比检索错误更伤信任)
MATCH_THRESHOLD = 0.90


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
                enabled INTEGER DEFAULT 1,
                source TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
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
                      source: str = "") -> Dict[str, Any]:
    """新增/更新标注:问题向量化(bge-m3 定死口径)与写入同事务落库。"""
    vec = app_state.llm_client.embeddings(
        [question], conv_id=None, stage="kg.annotation_embed")[0]
    _ensure_table(app_state, _kb_dim(app_state, kb_id))
    store = runtime.get_kg_store(app_state)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    aid = annotation_id or str(uuid.uuid4())
    vec_lit = "[" + ",".join(str(x) for x in vec) + "]"
    with store._get_conn() as conn:
        conn.execute(
            """INSERT INTO kg_annotations
               (id, kb_id, question, answer, question_vec, enabled, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?::vector, 1, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                 question = EXCLUDED.question, answer = EXCLUDED.answer,
                 question_vec = EXCLUDED.question_vec,
                 enabled = 1, source = EXCLUDED.source, updated_at = EXCLUDED.updated_at""",
            (aid, kb_id, question, answer, vec_lit, source, now, now))
    return {"id": aid, "kbId": kb_id, "question": question,
            "answer": answer, "enabled": True, "source": source}


def delete_annotation(app_state, kb_id: str, annotation_id: str) -> bool:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM kg_annotations WHERE id = ? AND kb_id = ?",
            (annotation_id, kb_id))
        return cur.rowcount > 0


def set_enabled(app_state, kb_id: str, annotation_id: str, enabled: bool) -> bool:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        cur = conn.execute(
            "UPDATE kg_annotations SET enabled = ?, updated_at = ? "
            "WHERE id = ? AND kb_id = ?",
            (1 if enabled else 0, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             annotation_id, kb_id))
        return cur.rowcount > 0


def list_annotations(app_state, kb_id: str) -> List[Dict[str, Any]]:
    store = runtime.get_kg_store(app_state)
    with store._get_conn() as conn:
        rows = conn.execute(
            """SELECT id, kb_id, question, answer, enabled, source, created_at, updated_at
               FROM kg_annotations WHERE kb_id = ? ORDER BY created_at DESC""",
            (kb_id,)).fetchall()
    return [{
        "id": r["id"], "kbId": r["kb_id"], "question": r["question"],
        "answer": r["answer"], "enabled": bool(r["enabled"]),
        "source": r["source"] or "", "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    } for r in rows]


def match_annotation(app_state, kb_id: str, query: str
                     ) -> Optional[Dict[str, Any]]:
    """问题向量相似度短路:命中(≥阈值)返回标注,未命中返回 None。

    answer 前置层——kb_search/搜索 REST 在走检索前调用本函数;
    命中直接以标注作答(不走 hybrid_retrieve),并带 annotationHit
    标记供前端/观测区分"这是标准答案"。
    """
    try:
        vec = app_state.llm_client.embeddings(
            [query], conv_id=None, stage="kg.annotation_query",
            mode="query")[0]
        _ensure_table(app_state, len(vec))
        vec_lit = "[" + ",".join(str(x) for x in vec) + "]"
        store = runtime.get_kg_store(app_state)
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
            "annotationHit": True,
        }
    except Exception as e:
        # 短路层故障不拖垮检索:记日志返回 None,回答走常规链路
        logger.warning(f"标注短路匹配失败(降级常规检索): {e}")
        return None
