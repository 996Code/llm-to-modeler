"""KGStore - 知识图谱插件的 PG 元数据层(kg_ 前缀三表)。

【表职责】
  kg_knowledge_bases  知识库本体:名称/描述/schema_json(类型体系)/
                      embedding 模型与维度/vector_enabled/状态
  kg_documents        文档:原始文件信息/内容指纹(content_hash)/
                      导入状态机/计数统计(chunk/实体/关系)
  kg_chunks           切块:文本与状态(status 兼作 chunk 级 checkpoint,
                      增量重跑跳过 done 块)

【import_status 状态机】
  uploaded → importing → succeeded | partial | failed
  (uploaded = 已上传未导入;partial = 部分块失败但整体可用;幂等重导
   从 content_hash 判定"内容没变且已成功 → 跳过")

【与平台的关系】同库不同表(与 ConversationStore 同一个 PG 库,见 services.db),
沿用"每方法新连接 + WAL"模式,平台零感知。
"""
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── P2#12 全文第三路:2-gram 分词前置(自建最小词典,不引 jieba) ──

# CJK 连续段(基本区;兼容字符由 doc_parser 出口 NFKC 归一化收敛)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]+")

# 最小停用词典:纯虚词组合的 gram 对召回只有噪声(问句里高频出现却
# 不指向任何实体),查询侧滤除。索引侧不滤(生成列无法感知查询语境)。
_GRAM_STOPWORDS = {
    "什么", "怎么", "如何", "哪里", "哪些", "为什么", "是不是", "有没有",
    "可以", "应该", "能够", "已经", "还是", "以及", "因此", "但是", "然后",
    "如果", "虽然", "的人", "的事",
}
_STOPCHARS = set(
    "的了吗呢吧啊呀哦嘛么之乎者也在和与跟对从被把让向往上下中"
    "里外个这那哪你我他她它谁就也都还又再很挺太更最要是着没不")

# 索引侧 2-gram 函数(生成列表达式引用,必须 IMMUTABLE):
# CJK 连续段切双字滑窗 gram,拉丁/数字段保留原词,空格拼接后交
# to_tsvector('simple')——'simple' 按空白切 lexeme,正好一 gram 一词。
# 单字 CJK 段(长度 1)无 bigram,跳过(单字噪声大于信号)。
_FULLTEXT_FN_DDL = """
CREATE OR REPLACE FUNCTION kg_cjk_ngrams(src text) RETURNS text
LANGUAGE sql IMMUTABLE AS $f$
  SELECT coalesce(string_agg(g, ' '), '')
  FROM (
    SELECT substring(r.run, i, 2) AS g
    FROM (SELECT (regexp_matches(src, '[一-鿿]{2,}', 'g'))[1] AS run) r
    CROSS JOIN LATERAL generate_series(1, length(r.run) - 1) AS i
    UNION ALL
    SELECT (regexp_matches(src, '[A-Za-z0-9]+', 'g'))[1] AS g
  ) grams
$f$
"""


def query_grams(query: str) -> List[str]:
    """查询 → 2-gram 词项(CJK 双字滑窗 + 拉丁/数字原词,停用滤除)。

    与索引侧 kg_cjk_ngrams 同源(gram 集合一致才能命中);额外做查询
    侧停用滤除与去重。空查询/纯停用 → [](调用方直接短路)。
    入口 NFKC 归一化:索引侧文本经 doc_parser 出口已 NFKC(全角→半角),
    查询不归一则全角"ＡＢＣ"/全角数字两边形态不一致,兜底静默零命中。
    """
    query = unicodedata.normalize("NFKC", query or "")
    grams: List[str] = []
    for run in _CJK_RUN_RE.findall(query or ""):
        grams.extend(run[i:i + 2] for i in range(len(run) - 1))
    grams.extend(t.lower() for t in _ALNUM_RUN_RE.findall(query or ""))
    out: List[str] = []
    for g in grams:
        if g in _GRAM_STOPWORDS:
            continue
        if len(g) == 2 and g[0] in _STOPCHARS and g[1] in _STOPCHARS:
            continue
        if g not in out:
            out.append(g)
    return out


class KGStore:
    """kg_* 三表的 DAO。"""

    def __init__(self, database_url: str = ""):
        # 存储走 SDK 关系型通道(pack 独立 schema + 共享平台池)——
        # pack 不直接 import 平台 services 层(架构方向:api → domains → sdk)
        from sdk.relational_store import PackRelationalDB
        self.db = PackRelationalDB("knowledge_graph", database_url or None)
        self._init_db()
        logger.info("KGStore initialized: postgres(schema=knowledge_graph)")

    def _get_conn(self):
        return self.db.connect()

    _DDL = [
        """CREATE TABLE IF NOT EXISTS kg_knowledge_bases (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            schema_json TEXT,               -- 本体:entity_types/relation_types/schema_mode/pending_types
            schema_template TEXT DEFAULT '',-- 建库所选模板 key(溯源)
            embedding_model TEXT DEFAULT '',
            vector_dim INTEGER,
            vector_enabled INTEGER DEFAULT 1,  -- 本地 bge-m3 定死能力,默认启用(10-15 裁决)
            kind TEXT DEFAULT 'kb',         -- kb(默认,存量兼容) | folder(目录,分组用)
            parent_id TEXT,                -- 层级树:NULL=顶层;指向 folder id(10-15 用户需求)
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS kg_documents (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT DEFAULT '',
            size_bytes INTEGER DEFAULT 0,
            file_path TEXT DEFAULT '',
            content_hash TEXT DEFAULT '',
            import_status TEXT DEFAULT 'uploaded',
            chunk_count INTEGER DEFAULT 0,
            entity_count INTEGER DEFAULT 0,
            relation_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_kg_docs_kb ON kg_documents(kb_id, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS kg_chunks (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL,
            char_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',  -- pending/done/failed(chunk 级 checkpoint)
            created_at TEXT NOT NULL
        )""",
    "CREATE INDEX IF NOT EXISTS idx_kg_chunks_doc ON kg_chunks(doc_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_kg_chunks_status ON kg_chunks(doc_id, status)",
    # 检索数据权限(嵌入模式):每库允许的用户/角色;空=全开放
    """CREATE TABLE IF NOT EXISTS kg_kb_acl (
        id TEXT PRIMARY KEY,
        kb_id TEXT NOT NULL,
        subject_type TEXT NOT NULL,   -- user | role
        subject_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (kb_id, subject_type, subject_id)
    )""",
    # 数据权限选择字典(宿主同步):用户/角色候选,ACL 编辑页下拉选择;
    # status 停用条目不进 ACL 候选
    """CREATE TABLE IF NOT EXISTS kg_subjects (
        subject_type TEXT NOT NULL,   -- user | role
        subject_id TEXT NOT NULL,
        display_name TEXT DEFAULT '', -- 展示名(可选;空则显示 subject_id)
        status TEXT DEFAULT 'active', -- active | disabled
        created_at TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (subject_type, subject_id)
    )""",
    # 目录同步/管理操作流水(全量/增量/单条 upsert/删除;只留最近 200 条)
    """CREATE TABLE IF NOT EXISTS kg_subject_sync_logs (
        id TEXT PRIMARY KEY,
        subject_type TEXT NOT NULL,
        mode TEXT NOT NULL,          -- full | delta | upsert | delete
        summary JSONB DEFAULT '{}'::jsonb,
        synced_at TEXT NOT NULL
    )""",
    ]

    def _init_db(self):
        self.db.init_schema(self._DDL)
        # D7 metadata 过滤:文档级元数据(扁平键值 {"部门":"销售"}),
        # 检索时按 pack_params 的 doc_filter 下推(嵌入模式宿主传入生效)
        try:
            with self._get_conn() as conn:
                conn.execute("ALTER TABLE kg_documents "
                             "ADD COLUMN IF NOT EXISTS doc_metadata JSONB DEFAULT '{}'::jsonb")
                conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_docs_metadata
                                ON kg_documents USING gin (doc_metadata jsonb_path_ops)""")
        except Exception as e:
            logger.warning(f"doc_metadata 列迁移失败(不影响启动,下次重试): {e}")
        # 层级树列迁移(10-15 用户需求:左侧树):存量表无 kind/parent_id
        try:
            with self._get_conn() as conn:
                conn.execute("ALTER TABLE kg_knowledge_bases "
                             "ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'kb'")
                conn.execute("ALTER TABLE kg_knowledge_bases "
                             "ADD COLUMN IF NOT EXISTS parent_id TEXT")
        except Exception as e:
            logger.warning(f"层级树列迁移失败(不影响启动,下次重试): {e}")
        # D33 建库向导:库级分段参数(建库时配置/设置页重配;JSON,键与
        # 插件设置同名)。NULL=未配置,导入时回退插件设置。
        try:
            with self._get_conn() as conn:
                conn.execute("ALTER TABLE kg_knowledge_bases "
                             "ADD COLUMN IF NOT EXISTS chunk_params TEXT")
        except Exception as e:
            logger.warning(f"库级分段参数列迁移失败(不影响启动,下次重试): {e}")
        # D29 块命中统计:kg_chunks 加 hit_count(被引用即 +1,
        # "哪些知识真被用"的治理数据)。默认 0,旧行迁移自动补。
        try:
            with self._get_conn() as conn:
                conn.execute("ALTER TABLE kg_chunks "
                             "ADD COLUMN IF NOT EXISTS hit_count INTEGER DEFAULT 0")
        except Exception as e:
            logger.warning(f"hit_count 列迁移失败(不影响启动,下次重试): {e}")
        # 用户/角色目录同步(全量+增量+CRUD):kg_subjects 补 status/
        # created_at;存量行回填 created_at=updated_at(首见时间不可考,取
        # 最近更新时间是最诚实的近似)。同步日志表同批落成。
        try:
            with self._get_conn() as conn:
                conn.execute("ALTER TABLE kg_subjects "
                             "ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")
                conn.execute("ALTER TABLE kg_subjects "
                             "ADD COLUMN IF NOT EXISTS created_at TEXT")
                conn.execute(
                    "UPDATE kg_subjects SET created_at = COALESCE(created_at, updated_at) "
                    "WHERE created_at IS NULL")
        except Exception as e:
            logger.warning(f"kg_subjects 目录列迁移失败(不影响启动,下次重试): {e}")
        # 一次性迁移:存量"未决"库(vector_enabled=0 且无 embedding_model
        # 记录——曾因探测失败落 0)翻转为启用——本地 bge-m3 定死能力。
        # 模型变更显式关闭的库(0 且有模型记录)不动:那是向量兼容性保护态。
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE kg_knowledge_bases SET vector_enabled = 1, "
                    "updated_at = ? WHERE vector_enabled = 0 AND "
                    "(embedding_model IS NULL OR embedding_model = '')",
                    (_now(),))
        except Exception as e:
            logger.warning(f"存量库向量启用迁移失败(不影响启动,下次重试): {e}")
        # P2#12 全文第三路:kg_chunks 加 tsvector 生成列 + GIN 索引。
        # 【偏差记录】蓝图原文是 to_tsvector('simple', text),但 PG 'simple'
        # 配置不切 CJK——连续汉字整句成单 lexeme(实测 '中华人民共和国民法典'
        # → 一个 token),2-gram 查询永不命中。故把"应用层中文分词前置"
        # 落成 IMMUTABLE SQL 函数 kg_cjk_ngrams(CJK 双字滑窗 + 拉丁/数字
        # 原词,空格分隔),生成列对其取 tsvector——分词仍在库外定义、
        # 不引 jieba/zhparser,与"自建最小词典前置应用层"的选型一致。
        # 【超时豁免】生成列对存量行全表重写(约 20ms/1200 字 chunk,500+
        # chunk 即超池级 statement_timeout=10s)——不豁免则迁移永久超时
        # 静默失败,每次重启重复持 ACCESS EXCLUSIVE 锁。SET LOCAL 只作用
        # 于本事务,不影响池里其他连接的护栏语义。
        try:
            with self._get_conn() as conn:
                conn.execute("SET LOCAL statement_timeout = '600s'")
                conn.execute(_FULLTEXT_FN_DDL)
                conn.execute(
                    "ALTER TABLE kg_chunks ADD COLUMN IF NOT EXISTS search_text tsvector "
                    "GENERATED ALWAYS AS "
                    "(to_tsvector('simple'::regconfig, kg_cjk_ngrams(text))) STORED")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_chunks_fulltext "
                    "ON kg_chunks USING GIN (search_text)")
        except Exception as e:
            logger.warning(f"全文索引迁移失败(不影响启动,下次重试): {e}")

    # ── 知识库 ─────────────────────────────────────────────

    # 目录层级上限(含库叶子):内部/技术依据/子分类/库=4 层足够;更深时
    # 左侧树在窄侧栏横向挤压,可读性崩——限深防患于源头,常量放开即扩
    MAX_TREE_DEPTH = 4

    def _kb_depth(self, kb: Dict[str, Any]) -> int:
        """节点深度(顶层=1):沿 parentId 链上溯计数;断链(父被删)按断点算。"""
        depth, hop, guard = 1, kb, 0
        while hop is not None and hop.get("parentId") and guard < 100:
            hop = self.get_kb(hop.get("parentId"))
            if hop is None:
                break
            depth += 1
            guard += 1
        return depth

    def _subtree_height(self, kb_id: str) -> int:
        """节点子树高度(自身=1):move 时整棵子树一起换深度,须整体校验。"""
        children = [k for k in self.list_kbs() if k.get("parentId") == kb_id]
        return 1 + (max(self._subtree_height(c["id"]) for c in children) if children else 0)

    def create_kb(
        self,
        name: str,
        description: str = "",
        schema_json: Optional[Dict] = None,
        schema_template: str = "",
        kind: str = "kb",
        parent_id: Optional[str] = None,
        chunk_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if kind not in ("kb", "folder"):
            raise ValueError(f"kind 须为 kb|folder,实际: {kind}")
        if parent_id:
            parent = self.get_kb(parent_id)
            if not parent:
                raise ValueError(f"父节点不存在: {parent_id}")
            if parent.get("kind") != "folder":
                raise ValueError("parent 必须是 folder(目录),不能挂在知识库下")
            if self._kb_depth(parent) >= self.MAX_TREE_DEPTH:
                raise ValueError(
                    f"目录层级已达上限({self.MAX_TREE_DEPTH} 层),不能再往下挂")
        kb_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO kg_knowledge_bases
                   (id, name, description, schema_json, schema_template,
                    vector_enabled, kind, parent_id, chunk_params,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kb_id, name, description,
                 json.dumps(schema_json, ensure_ascii=False) if schema_json else None,
                 schema_template, 1 if kind == "kb" else 0,
                 kind, parent_id,
                 json.dumps(chunk_params, ensure_ascii=False) if chunk_params else None,
                 now, now),
            )
        return self.get_kb(kb_id)

    def list_kb_tree(self) -> List[Dict[str, Any]]:
        """层级树(10-15 用户需求):顶层(含目录与散库)在前,目录下挂 children。

        folder 不带文档统计(它不存文档);kb 带 docCount/entityTotal。
        排序:folder 先于 kb,同级按名称。
        """
        flat = self.list_kbs()
        by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for item in flat:
            by_parent.setdefault(item.get("parentId"), []).append(item)
        def build(parent_id: Optional[str]) -> List[Dict[str, Any]]:
            kids = by_parent.get(parent_id, [])
            kids.sort(key=lambda x: (0 if x.get("kind") == "folder" else 1, x["name"]))
            for k in kids:
                k["children"] = build(k["id"]) if k.get("kind") == "folder" else []
            return kids
        return build(None)

    def move_kb(self, kb_id: str, parent_id: Optional[str]) -> bool:
        """移动节点(换父/提顶层)。环防护:新父链上不得包含自身。"""
        node = self.get_kb(kb_id)
        if not node:
            return False
        if parent_id:
            parent = self.get_kb(parent_id)
            if not parent:
                raise ValueError(f"父节点不存在: {parent_id}")
            if parent.get("kind") != "folder":
                raise ValueError("parent 必须是 folder(目录)")
            # 环检测:沿 parent 链上溯,链上任一节点是自身即环
            # (f2 的祖先是 f1;把 f1 移到 f2 下 = f1 成了自己的祖先)
            hop, guard = parent, 0
            while hop is not None and guard < 100:
                if hop["id"] == kb_id:
                    raise ValueError("形成环(cycle):不能把目录移到自己的子树下")
                pid = hop.get("parentId")
                hop = self.get_kb(pid) if pid else None
                guard += 1
            # 限深:整棵子树跟着挪,新父深度+子树高度不得超上限
            if self._kb_depth(parent) + self._subtree_height(kb_id) > self.MAX_TREE_DEPTH:
                raise ValueError(
                    f"移动后目录层级将超过上限({self.MAX_TREE_DEPTH} 层)")
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE kg_knowledge_bases SET parent_id = ?, updated_at = ? "
                "WHERE id = ?", (parent_id, _now(), kb_id))
            return cur.rowcount > 0

    def get_kb(self, kb_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
        return self._kb_row(row) if row else None

    def get_kb_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_knowledge_bases WHERE name = ?", (name,)
            ).fetchone()
        return self._kb_row(row) if row else None

    def list_kbs(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT b.*,
                          (SELECT COUNT(*) FROM kg_documents d WHERE d.kb_id = b.id) AS doc_count,
                          (SELECT COALESCE(SUM(d.entity_count), 0) FROM kg_documents d
                            WHERE d.kb_id = b.id) AS entity_total,
                          (SELECT COALESCE(SUM(d.relation_count), 0) FROM kg_documents d
                            WHERE d.kb_id = b.id) AS relation_total
                     FROM kg_knowledge_bases b
                    ORDER BY b.created_at DESC"""
            ).fetchall()
        result = []
        for r in rows:
            kb = self._kb_row(r)
            kb["docCount"] = int(r["doc_count"])
            kb["entityTotal"] = int(r["entity_total"])
            kb["relationTotal"] = int(r["relation_total"])
            result.append(kb)
        return result

    def update_kb(
        self, kb_id: str, name: Optional[str] = None, description: Optional[str] = None,
        schema_json: Optional[Dict] = None, chunk_params: Optional[Dict] = None,
    ) -> bool:
        sets, params = ["updated_at = ?"], [_now()]
        if name is not None:
            sets.append("name = ?"); params.append(name)
        if description is not None:
            sets.append("description = ?"); params.append(description)
        if schema_json is not None:
            sets.append("schema_json = ?")
            params.append(json.dumps(schema_json, ensure_ascii=False))
        if chunk_params is not None:
            sets.append("chunk_params = ?")
            params.append(json.dumps(chunk_params, ensure_ascii=False))
        params.append(kb_id)
        with self._get_conn() as conn:
            cur = conn.execute(
                f"UPDATE kg_knowledge_bases SET {', '.join(sets)} WHERE id = ?", params)
            return cur.rowcount > 0

    def set_kb_vector_info(
        self, kb_id: str, embedding_model: str, vector_dim: int, vector_enabled: bool,
    ) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                """UPDATE kg_knowledge_bases
                   SET embedding_model = ?, vector_dim = ?, vector_enabled = ?, updated_at = ?
                   WHERE id = ?""",
                (embedding_model, vector_dim, 1 if vector_enabled else 0, _now(), kb_id),
            )
            return cur.rowcount > 0

    # ── 检索数据权限 ACL(嵌入模式) ───────────────────────

    def set_kb_acl(self, kb_id: str, users: List[str], roles: List[str]) -> None:
        """全量替换某库的 ACL(管理端/接口写入)。空列表=清空=回到全开放。"""
        now = _now()
        rows = [(str(uuid.uuid4()), kb_id, "user", u, now) for u in users] + \
               [(str(uuid.uuid4()), kb_id, "role", r, now) for r in roles]
        with self._get_conn() as conn:
            conn.execute("DELETE FROM kg_kb_acl WHERE kb_id = ?", (kb_id,))
            conn.executemany(
                "INSERT INTO kg_kb_acl (id, kb_id, subject_type, subject_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)", rows)

    def get_kb_acl(self, kb_id: str) -> Dict[str, List[str]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT subject_type, subject_id FROM kg_kb_acl "
                "WHERE kb_id = ? ORDER BY created_at", (kb_id,)).fetchall()
        users = [r["subject_id"] for r in rows if r["subject_type"] == "user"]
        roles = [r["subject_id"] for r in rows if r["subject_type"] == "role"]
        return {"users": users, "roles": roles}

    def get_all_kb_acl(self) -> Dict[str, List[Dict[str, str]]]:
        """全量 ACL(检索过滤一次读齐,逐库查会有 N 次往返)。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT kb_id, subject_type, subject_id FROM kg_kb_acl").fetchall()
        out: Dict[str, List[Dict[str, str]]] = {}
        for r in rows:
            out.setdefault(r["kb_id"], []).append(
                {"subjectType": r["subject_type"], "subjectId": r["subject_id"]})
        return out

    # ── 用户/角色目录(源系统同步 + 单条管理;ACL 候选字典) ──
    # 对外契约(全量/增量/CRUD/同步状态)见 doc/知识库用户角色同步API.md。

    def sync_subjects_full(self, subject_type: str,
                           items: List[Dict[str, Any]]) -> Dict[str, int]:
        """全量替换某类型目录——源系统推送权威快照。

        items: [{id, name?, status?}](api 层已校验去重)。清单外旧条目
        删除(源侧删人这里跟着走,ACL 下拉不残留幽灵);值未变的行不写
        (updated_at 不假跳,重推同清单完全幂等)。整个替换一个事务,
        中途失败全部回滚。
        """
        now = _now()
        created = updated = unchanged = 0
        with self._get_conn() as conn:
            current = self._subjects_map(conn, subject_type)
            keep = set()
            for it in items:
                sid = it["id"]
                keep.add(sid)
                name = it.get("name") or ""
                status = it.get("status") or "active"
                old = current.get(sid)
                if old is None:
                    created += 1
                    conn.execute(
                        "INSERT INTO kg_subjects (subject_type, subject_id, "
                        "display_name, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (subject_type, sid, name, status, now, now))
                elif self._subject_changed(old, name, status):
                    updated += 1
                    conn.execute(
                        "UPDATE kg_subjects SET display_name = ?, "
                        "status = ?, updated_at = ? "
                        "WHERE subject_type = ? AND subject_id = ?",
                        (name, status, now, subject_type, sid))
                else:
                    unchanged += 1
            removed = [sid for sid in current if sid not in keep]
            if removed:
                conn.executemany(
                    "DELETE FROM kg_subjects "
                    "WHERE subject_type = ? AND subject_id = ?",
                    [(subject_type, sid) for sid in removed])
            summary = {"received": len(items), "created": created,
                       "updated": updated, "unchanged": unchanged,
                       "removed": len(removed)}
            self._log_subject_sync(conn, subject_type, "full", summary, now)
        return summary

    def sync_subjects_delta(self, subject_type: str,
                            upserts: List[Dict[str, Any]],
                            deletes: List[str]) -> Dict[str, Any]:
        """增量同步:upserts 按 id 合并(缺省字段保持现值),deletes 如实报缺失。

        重放安全:upsert 值未变不写(幂等);删除不存在的 id 进 missing 不
        报错——源系统重推已消费过的增量不会失败。整批一个事务。同批
        upsert 又 delete 同一 id 时 delete 生效(顺序语义,建议避免)。
        """
        now = _now()
        inserted = updated = unchanged = deleted = 0
        with self._get_conn() as conn:
            current = self._subjects_map(conn, subject_type)
            for it in upserts:
                sid = it["id"]
                old = current.get(sid)
                if old is None:
                    inserted += 1
                    name = it.get("name") or ""
                    status = it.get("status") or "active"
                    conn.execute(
                        "INSERT INTO kg_subjects (subject_type, subject_id, "
                        "display_name, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (subject_type, sid, name, status, now, now))
                    current[sid] = {"display_name": name, "status": status}
                else:
                    name = (it["name"] if it.get("name") is not None
                            else old["display_name"] or "")
                    status = it.get("status") or old["status"] or "active"
                    if self._subject_changed(old, name, status):
                        updated += 1
                        conn.execute(
                            "UPDATE kg_subjects SET display_name = ?, "
                            "status = ?, updated_at = ? "
                            "WHERE subject_type = ? AND subject_id = ?",
                            (name, status, now, subject_type, sid))
                        current[sid] = {"display_name": name, "status": status}
                    else:
                        unchanged += 1
            hit = [sid for sid in deletes if sid in current]
            if hit:
                conn.executemany(
                    "DELETE FROM kg_subjects "
                    "WHERE subject_type = ? AND subject_id = ?",
                    [(subject_type, sid) for sid in hit])
                deleted = len(hit)
            missing = [sid for sid in deletes if sid not in current]
            summary = {"upserts": len(upserts), "inserted": inserted,
                       "updated": updated, "unchanged": unchanged,
                       "deletes": len(deletes), "deleted": deleted,
                       "missing": missing}
            self._log_subject_sync(conn, subject_type, "delta", summary, now)
        return summary

    def upsert_subject(self, subject_type: str, subject_id: str,
                       fields: Dict[str, Any]) -> Dict[str, Any]:
        """单条 upsert(建/改合一):缺省字段=新建用默认/更新保现值。

        返回落库记录 + created 标记(是否本次新建)。
        """
        now = _now()
        with self._get_conn() as conn:
            old = self._subjects_map(conn, subject_type).get(subject_id)
            created = old is None
            if created:
                conn.execute(
                    "INSERT INTO kg_subjects (subject_type, subject_id, "
                    "display_name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (subject_type, subject_id, fields.get("name") or "",
                     fields.get("status") or "active", now, now))
            else:
                name = (fields["name"] if fields.get("name") is not None
                        else old["display_name"] or "")
                status = fields.get("status") or old["status"] or "active"
                if self._subject_changed(old, name, status):
                    conn.execute(
                        "UPDATE kg_subjects SET display_name = ?, "
                        "status = ?, updated_at = ? "
                        "WHERE subject_type = ? AND subject_id = ?",
                        (name, status, now, subject_type, subject_id))
            row = conn.execute(
                "SELECT * FROM kg_subjects "
                "WHERE subject_type = ? AND subject_id = ?",
                (subject_type, subject_id)).fetchone()
            self._log_subject_sync(conn, subject_type, "upsert",
                                   {"id": subject_id, "created": created}, now)
        return {**self._subject_out(row), "created": created}

    def delete_subject(self, subject_type: str, subject_id: str) -> bool:
        now = _now()
        with self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM kg_subjects "
                "WHERE subject_type = ? AND subject_id = ?",
                (subject_type, subject_id))
            ok = cur.rowcount > 0
            if ok:
                self._log_subject_sync(conn, subject_type, "delete",
                                       {"id": subject_id}, now)
        return ok

    def get_subject(self, subject_type: str,
                    subject_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_subjects "
                "WHERE subject_type = ? AND subject_id = ?",
                (subject_type, subject_id)).fetchone()
        return self._subject_out(row) if row else None

    def list_subjects_page(self, subject_type: str, q: str = "",
                           status: str = "", page: Optional[int] = None,
                           page_size: int = 50) -> Dict[str, Any]:
        """目录列表:q 模糊匹配 id/名称,status 过滤;page 传了才分页。

        不传 page 返回全部(小字典全量拉取的兼容口径)。
        """
        where = ["subject_type = ?"]
        params: List[Any] = [subject_type]
        if q:
            like = f"%{q}%"
            where.append("(subject_id ILIKE ? OR display_name ILIKE ?)")
            params += [like, like]
        if status:
            where.append("status = ?")
            params.append(status)
        cond = " AND ".join(where)
        with self._get_conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM kg_subjects WHERE {cond}",
                tuple(params)).fetchone()["n"]
            sql = f"SELECT * FROM kg_subjects WHERE {cond} ORDER BY subject_id"
            if page is not None:
                sql += " LIMIT ? OFFSET ?"
                params += [page_size, (page - 1) * page_size]
            rows = conn.execute(sql, tuple(params)).fetchall()
        return {"items": [self._subject_out(r) for r in rows], "total": total,
                "page": page, "pageSize": page_size if page is not None else None}

    def subjects_sync_status(self) -> Dict[str, Any]:
        """目录同步状态总览:各类计数(总/在用/停用)+ 最近一次同步 + 流水。"""
        with self._get_conn() as conn:
            out: Dict[str, Any] = {}
            for t in ("user", "role"):
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM kg_subjects "
                    "WHERE subject_type = ? GROUP BY status", (t,)).fetchall()
                by = {r["status"]: r["n"] for r in rows}
                out[t] = {"total": sum(by.values()),
                          "active": by.get("active", 0),
                          "disabled": by.get("disabled", 0)}
            for r in conn.execute(
                    "SELECT DISTINCT ON (subject_type) subject_type, mode, synced_at "
                    "FROM kg_subject_sync_logs "
                    "ORDER BY subject_type, synced_at DESC").fetchall():
                out[r["subject_type"]]["lastSyncAt"] = r["synced_at"]
                out[r["subject_type"]]["lastMode"] = r["mode"]
            recent = conn.execute(
                "SELECT subject_type, mode, summary, synced_at "
                "FROM kg_subject_sync_logs "
                "ORDER BY synced_at DESC LIMIT 20").fetchall()
        out["recent"] = [{"subjectType": r["subject_type"], "mode": r["mode"],
                          "summary": r["summary"] or {},
                          "syncedAt": r["synced_at"]} for r in recent]
        return out

    def _subjects_map(self, conn,
                      subject_type: str) -> Dict[str, Dict[str, Any]]:
        """某类型目录全量快照(id → {display_name, status});同步 diff 用。"""
        return {r["subject_id"]: {"display_name": r["display_name"],
                                  "status": r["status"] or "active"}
                for r in conn.execute(
                    "SELECT subject_id, display_name, status "
                    "FROM kg_subjects WHERE subject_type = ?",
                    (subject_type,)).fetchall()}

    @staticmethod
    def _subject_changed(old: Dict[str, Any], name: str,
                         status: str) -> bool:
        return ((old["display_name"] or "") != name
                or (old["status"] or "active") != status)

    @staticmethod
    def _subject_out(r) -> Dict[str, Any]:
        """行 → 对外形状(name 空则回退显示 id,与 ACL 候选口径一致)。"""
        return {"id": r["subject_id"],
                "name": r["display_name"] or r["subject_id"],
                "status": r["status"] or "active",
                "createdAt": r["created_at"] or "",
                "updatedAt": r["updated_at"]}

    def _log_subject_sync(self, conn, subject_type: str, mode: str,
                          summary: Dict[str, Any], now: str) -> None:
        """同步/管理操作流水(同事务写入;只留最近 200 条防膨胀)。"""
        conn.execute(
            "INSERT INTO kg_subject_sync_logs "
            "(id, subject_type, mode, summary, synced_at) "
            "VALUES (?, ?, ?, ?::jsonb, ?)",
            (str(uuid.uuid4()), subject_type, mode,
             json.dumps(summary, ensure_ascii=False), now))
        conn.execute(
            "DELETE FROM kg_subject_sync_logs WHERE id NOT IN ("
            "SELECT id FROM kg_subject_sync_logs "
            "ORDER BY synced_at DESC LIMIT 200)")

    def list_subjects(self, subject_type: str) -> List[Dict[str, str]]:
        """活跃目录(ACL 编辑页下拉候选;停用条目不出现)。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT subject_id, display_name FROM kg_subjects "
                "WHERE subject_type = ? AND status = 'active' "
                "ORDER BY subject_id", (subject_type,)).fetchall()
        return [{"id": r["subject_id"],
                 "name": r["display_name"] or r["subject_id"]} for r in rows]

    def delete_kb(self, kb_id: str) -> bool:
        """级联删元数据(图/向量由调用方清理)。"""
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM kg_knowledge_bases WHERE id = ?", (kb_id,))
            conn.execute("DELETE FROM kg_chunks WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kg_documents WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kg_kb_acl WHERE kb_id = ?", (kb_id,))
            # kg_annotations 懒建(annotations.py 首用才建),未建过的库跳过;
            # to_regclass 走事务级 search_path,只读不中断事务
            ann_exists = conn.execute(
                "SELECT to_regclass('kg_annotations') IS NOT NULL AS ok").fetchone()
            if ann_exists and ann_exists["ok"]:
                conn.execute("DELETE FROM kg_annotations WHERE kb_id = ?", (kb_id,))
            return cur.rowcount > 0

    @staticmethod
    def _kb_row(row: dict) -> Dict[str, Any]:
        d = dict(row)
        schema = None
        if d.get("schema_json"):
            try:
                schema = json.loads(d["schema_json"])
            except (ValueError, TypeError):
                schema = None
        # 库级分段参数(D33 建库向导):坏 JSON 不炸启动,按未配置回退
        chunk_params = None
        if d.get("chunk_params"):
            try:
                chunk_params = json.loads(d["chunk_params"])
            except (ValueError, TypeError):
                chunk_params = None
        return {
            "id": d["id"], "name": d["name"], "description": d.get("description") or "",
            "schema": schema, "schemaTemplate": d.get("schema_template") or "",
            "embeddingModel": d.get("embedding_model") or "",
            "vectorDim": d.get("vector_dim"),
            "vectorEnabled": bool(d.get("vector_enabled")),
            # 层级树(10-15):kind=kb|folder;parentId 指向 folder(顶层为 None)
            "kind": d.get("kind") or "kb",
            "parentId": d.get("parent_id"),
            "status": d.get("status") or "active",
            # D33:库级分段参数(None=未配置,导入回退插件设置)
            "chunkParams": chunk_params,
            "createdAt": d["created_at"], "updatedAt": d["updated_at"],
        }

    # ── 文档 ───────────────────────────────────────────────

    def create_document(
        self, kb_id: str, filename: str, mime_type: str, size_bytes: int,
        file_path: str, content_hash: str,
    ) -> Dict[str, Any]:
        doc_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO kg_documents
                   (id, kb_id, filename, mime_type, size_bytes, file_path,
                    content_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, kb_id, filename, mime_type, size_bytes, file_path,
                 content_hash, now, now),
            )
        return self.get_document(doc_id)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return self._doc_row(row) if row else None

    def get_document_by_hash(self, kb_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        """同库同内容文件查重(增量判定辅助)。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_documents WHERE kb_id = ? AND content_hash = ?",
                (kb_id, content_hash),
            ).fetchone()
        return self._doc_row(row) if row else None

    def list_documents(self, kb_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM kg_documents WHERE kb_id = ? ORDER BY created_at DESC",
                (kb_id,),
            ).fetchall()
        return [self._doc_row(r) for r in rows]

    def update_document(self, doc_id: str, **fields) -> bool:
        allowed = {
            "import_status", "chunk_count", "entity_count", "relation_count",
            "error", "content_hash", "filename", "file_path",
            "size_bytes", "mime_type",
        }
        sets, params = ["updated_at = ?"], [_now()]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"kg_documents column not updatable: {k}")
            sets.append(f"{k} = ?"); params.append(v)
        params.append(doc_id)
        with self._get_conn() as conn:
            cur = conn.execute(
                f"UPDATE kg_documents SET {', '.join(sets)} WHERE id = ?", params)
            return cur.rowcount > 0

    def delete_document(self, doc_id: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM kg_documents WHERE id = ?", (doc_id,))
            conn.execute("DELETE FROM kg_chunks WHERE doc_id = ?", (doc_id,))
            return cur.rowcount > 0

    def recover_importing_docs(self, active_doc_ids) -> List[Dict[str, Any]]:
        """启动恢复:没有存活任务支撑的 importing 文档收敛为 failed。

        返回中断文档清单(id/kbId)——调用方据此自动重新入队续跑(幂等,
        已完成块在 chunk checkpoint 里,重跑只补剩余部分)。
        active_doc_ids = 当前确实 pending/running 的导入任务对应的 doc_id
        (热切换重装配时活任务不能被误伤)。
        """
        with self._get_conn() as conn:
            stale_rows = conn.execute(
                "SELECT id, kb_id FROM kg_documents WHERE import_status = 'importing'",
            ).fetchall()
            conn.execute(
                """UPDATE kg_documents
                   SET import_status = 'failed',
                       error = '服务重启导致导入中断(可重新发起,已完成块会续跑)',
                       updated_at = ?
                   WHERE import_status = 'importing'""",
                (_now(),),
            )
            # 排除活任务:先整体收敛再放回 importing(集合小,两步比拼 NOT IN 简单)。
            # LIKE 模式必须走参数:SQL 文本里的字面 '%' 会被 psycopg 当占位符扫描报错
            for doc_id in active_doc_ids:
                conn.execute(
                    """UPDATE kg_documents SET import_status = 'importing', error = '',
                           updated_at = ? WHERE id = ? AND import_status = 'failed'
                           AND error LIKE ?""",
                    (_now(), doc_id, "服务重启导致导入中断%"),
                )
            active = set(active_doc_ids)
            return [{"id": r["id"], "kbId": r["kb_id"]}
                    for r in stale_rows if r["id"] not in active]


    def list_orphan_import_docs(self, active_doc_ids,
                                grace_seconds: int = 300) -> List[Dict[str, Any]]:
        """启动恢复兜底:无活任务支撑的待导文档(文档表为真相源)。

        覆盖两类孤儿:
          - uploaded 且超过宽限期(刚上传、任务提交在途的不误伤——
            热切换重装配时上传可与恢复并发);
          - 因重启中断收敛为 failed 的(error 前缀是 recover_importing_docs
            的固定标记;业务性失败不在此列,不会被重排)。
        调用方仍需按 active_doc_ids 过滤一遍(双保险)。
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=grace_seconds)).isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT id, kb_id FROM kg_documents
                   WHERE (import_status = 'uploaded' AND created_at < ?)
                      OR (import_status = 'failed' AND error LIKE ?)""",
                (cutoff, "服务重启导致导入中断%")).fetchall()
        return [{"id": r["id"], "kbId": r["kb_id"]}
                for r in rows if r["id"] not in active_doc_ids]

    def document_file_path(self, doc_id: str) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT file_path FROM kg_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return row["file_path"] if row else None

    @staticmethod
    def _doc_row(row: dict) -> Dict[str, Any]:
        d = dict(row)
        md = d.get("doc_metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (ValueError, TypeError):
                md = {}
        return {
            "id": d["id"], "kbId": d["kb_id"], "filename": d["filename"],
            "mimeType": d.get("mime_type") or "", "sizeBytes": d.get("size_bytes") or 0,
            "filePath": d.get("file_path") or "",
            "contentHash": d.get("content_hash") or "",
            "importStatus": d.get("import_status") or "uploaded",
            "chunkCount": d.get("chunk_count") or 0,
            "entityCount": d.get("entity_count") or 0,
            "relationCount": d.get("relation_count") or 0,
            "error": d.get("error") or "",
            "docMetadata": md if isinstance(md, dict) else {},
            "createdAt": d["created_at"], "updatedAt": d["updated_at"],
        }

    def set_doc_metadata(self, doc_id: str, kb_id: str,
                         metadata: Dict[str, Any]) -> bool:
        """D7:设置文档元数据(全量替换;空对象=清除过滤维度)。"""
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE kg_documents SET doc_metadata = ?::jsonb, updated_at = ? "
                "WHERE id = ? AND kb_id = ?",
                (json.dumps(metadata, ensure_ascii=False), _now(), doc_id, kb_id))
            return cur.rowcount > 0

    def filter_doc_ids_by_metadata(
        self, kb_id: str, doc_filter: Dict[str, Any],
    ) -> Optional[set]:
        """按元数据过滤返回允许的 doc_id 集合。

        返回 None = 不过滤(调用方未传/过滤条件为空);返回 set(可空)=
        白名单语义:空集表示没有文档匹配(检索路应零召回,不是全放行)。
        过滤语义:扁平键值全匹配(AND);值支持单值或数组(OR 任一)。
        """
        if not doc_filter:
            return None
        clauses, params = [], []
        for key, val in doc_filter.items():
            vals = val if isinstance(val, list) else [val]
            vals = [str(v) for v in vals if v is not None and str(v) != ""]
            if not vals:
                continue
            if len(vals) == 1:
                clauses.append("doc_metadata ->> %s = %s")
                params.extend([key, vals[0]])
            else:
                ph = ", ".join(["%s"] * len(vals))
                clauses.append(f"doc_metadata ->> %s IN ({ph})")
                params.append(key)
                params.extend(vals)
        if not clauses:
            return None
        sql = ("SELECT id FROM kg_documents WHERE kb_id = %s AND "
               + " AND ".join(clauses))
        with self._get_conn() as conn:
            rows = conn.execute(sql, [kb_id] + params).fetchall()
        return {r["id"] for r in rows}

    # ── 切块(兼 chunk 级 checkpoint) ───────────────────────

    def replace_chunks(
        self, doc_id: str, kb_id: str, chunks: List[Dict[str, Any]],
    ) -> int:
        """重建文档的切块(重导入路径:先删后插,seq 从 0 连续)。"""
        now = _now()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM kg_chunks WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                """INSERT INTO kg_chunks (id, kb_id, doc_id, seq, text, char_count, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                [(str(uuid.uuid4()), kb_id, doc_id, c["seq"], c["text"],
                  c.get("char_count", len(c["text"])), now) for c in chunks],
            )
            conn.execute(
                "UPDATE kg_documents SET chunk_count = ?, updated_at = ? WHERE id = ?",
                (len(chunks), now, doc_id),
            )
        return len(chunks)

    def bump_chunk_hits(self, chunk_ids: List[str]) -> int:
        """D29 块命中计数:被 LLM 引用([片段:N])的块 hit_count+1。

        单条 UPDATE ... IN 语句,失败静默(统计项不拖垮检索主链路)。
        返回实际更新的行数。
        """
        if not chunk_ids:
            return 0
        try:
            with self._get_conn() as conn:
                ph = ",".join("?" for _ in chunk_ids)
                cur = conn.execute(
                    f"UPDATE kg_chunks SET hit_count = COALESCE(hit_count, 0) + 1 "
                    f"WHERE id IN ({ph})", chunk_ids)
                return cur.rowcount
        except Exception as e:
            logger.warning(f"hit_count 更新失败(统计项,不阻断): {e}")
            return 0

    def upsert_summary_chunk(self, doc_id: str, kb_id: str, text: str) -> Optional[str]:
        """D14 摘要块:写入/更新文档级摘要块(seq=-1,status=done)。

        摘要块是"抽象问法兜底"索引——"劳动法主要讲什么"这类无具体实体
        的问法靠它命中。先删旧摘要再插(重导幂等);不计入 chunk_count
        (那是抽取块口径,摘要不是切块产物)。
        """
        now = _now()
        chunk_id = str(uuid.uuid4())
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM kg_chunks WHERE doc_id = ? AND seq = -1", (doc_id,))
            conn.execute(
                """INSERT INTO kg_chunks (id, kb_id, doc_id, seq, text, char_count, status, created_at)
                   VALUES (?, ?, ?, -1, ?, ?, 'done', ?)""",
                (chunk_id, kb_id, doc_id, text, len(text), now))
        return chunk_id

    def list_chunks(self, doc_id: str, status: Optional[str] = None,
                    include_summary: bool = False) -> List[Dict[str, Any]]:
        """文档块列表。include_summary=False(默认)排除摘要块(seq=-1)——
        抽取块口径(total/pending/续跑判定)不得被 D14 摘要块污染(审查修复:
        摘要曾计入 total 让进度分母多 1、断点续跑误判有已完成块)。"""
        sql = "SELECT * FROM kg_chunks WHERE doc_id = ?"
        params: list = [doc_id]
        if not include_summary:
            sql += " AND seq >= 0"
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY seq ASC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": r["id"], "docId": r["doc_id"], "kbId": r["kb_id"],
            "seq": int(r["seq"]), "text": r["text"],
            "charCount": int(r["char_count"] or 0),
            "status": r["status"],
            "hitCount": int(r["hit_count"] or 0) if "hit_count" in r.keys() else 0,
        } for r in rows]

    def get_chunks_by_ids(self, chunk_ids: List[str]) -> List[Dict[str, Any]]:
        """按 id 批量取块(父子回带:向量命中子块拆父 id 后回查父块全文)。

        chunk_id 为 uuid 全局唯一,无需 kb 过滤;返回结构与 list_chunks 同构。
        """
        if not chunk_ids:
            return []
        ph = ",".join("?" * len(chunk_ids))
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, doc_id, kb_id, seq, text, char_count, status, hit_count "
                f"FROM kg_chunks WHERE id IN ({ph})", list(chunk_ids)).fetchall()
        return [{
            "id": r["id"], "docId": r["doc_id"], "kbId": r["kb_id"],
            "seq": int(r["seq"]), "text": r["text"],
            "charCount": int(r["char_count"] or 0),
            "status": r["status"],
            "hitCount": int(r["hit_count"] or 0) if "hit_count" in r.keys() else 0,
        } for r in rows]

    def mark_chunk(self, chunk_id: str, status: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE kg_chunks SET status = ? WHERE id = ?", (status, chunk_id))
            return cur.rowcount > 0

    def update_chunk_text(self, chunk_id: str, text: str) -> bool:
        """块编辑(D27):更新块文本。search_text 是生成列,UPDATE 时
        PG 自动重算——全文检索随编辑即时同步,无需额外维护。"""
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE kg_chunks SET text = ?, char_count = ? WHERE id = ?",
                (text, len(text), chunk_id))
            return cur.rowcount > 0

    def search_chunks_fulltext(
        self, kb_id: str, query: str, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """PG 全文检索 kg_chunks(P2#12 第三路:tsvector + GIN + ts_rank)。

        query 经 query_grams 2-gram 分词,OR 拼 ts_query;命中按 ts_rank
        降序(同分按 seq 稳定)。返回与向量路 hits 同构:
        {chunkId, docId, seq, text, score, source='fulltext'}——
        docName/source 由检索编排层(retrieval)补注。
        """
        grams = query_grams(query)
        if not grams or limit <= 0:
            return []
        tsq = " | ".join(f"'{g}'" for g in grams)
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT c.id, c.doc_id, c.seq, c.text, c.char_count,
                          ts_rank(c.search_text, qs.q) AS rank
                     FROM kg_chunks c
                     JOIN (SELECT to_tsquery('simple'::regconfig, ?) AS q) qs
                       ON true
                    WHERE c.kb_id = ? AND c.search_text @@ qs.q
                    ORDER BY rank DESC, c.seq ASC
                    LIMIT ?""",
                (tsq, kb_id, int(limit)),
            ).fetchall()
        return [{
            "chunkId": r["id"], "docId": r["doc_id"], "seq": int(r["seq"]),
            "text": r["text"], "charCount": int(r["char_count"] or 0),
            "score": float(r["rank"]), "source": "fulltext",
        } for r in rows]
