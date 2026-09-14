"""chatbi Agent 记忆栈 —— 记忆存储 / 相关性召回 / LLM 提炼与整理 / 链路经验沉淀。

【模块定位】
Agent 跨会话记忆的完整闭环(自 chat-bi T037/E1 忠实移植):
  - 写入: LLM 自主提炼(extract_memory_from_turn, 判定"值得记"再落库) +
    程序化沉淀(persist_linkage_memory 表关联经验 / save_query_memory 查询流水);
  - 召回: recall_memories 关键词重叠排序(不调 LLM, 省调用) →
    recall_text 格式化为 SQL 生成 prompt 的【相关记忆】注入段(宁缺毋滥,
    无相关记忆返回空串);
  - 管理: list_memories / delete_memory 管理端列表与删除(api.py 记忆端点的后端);
  - 整理: consolidate_memories LLM 合并去重碎片记忆(原记忆标记隐藏, 可追溯);
  - 消费: ChatBIMemoryStore 供 graph_infer.sync_linkage_to_graph 以鸭子类型
    (list_memories() -> list[dict]) 读取 linkage 共现经验。

【设计】
  - 存储: 源"一个记忆一个 .md 文件 + MEMORY.md 索引" → 一行
    chatbi_agent_memories(id=UUID 不可变主键, name=可编辑标题,
    description=召回摘要)。表本身即索引(单一权威源), 幽灵索引条目在行存储下
    不复存在;reconcile_index 保留为 linkage 同名去重的幂等防御入口;
    MEMORY.md 的文本形态由 read_index() 等价生成(管理/调试用)。
  - 召回排序: 与源一致不调 LLM——description+name 与问题做词重叠度
    (英文 \\b 分词 + 中文单字)排序取 top_k, 零 API 成本;consolidated 与
    linkage(结构化数据)不进 prompt。
  - 失败降级(fail-open, 与源调用侧 try/except 语义一致): 召回/存储异常 →
    recall_text 返回 "";LLM 挂 → extract 返回 None;记忆提炼失败不阻断主流程。
  - 隔离维度: 源 memory/{tenant_id}/{data_source_id}/ 目录隔离 → tenant 维度
    删除,宿主身份以 user_id 列承担(可选过滤,不传即全量)。
  - LLM 注入: 函数参数 llm(.chat 返回 (content, meta);.chat_json 返回 dict),
    stage="chatbi.memory.*";召回路径零 LLM 调用, llm 参数仅为统一注入约定保留。
  - 同步实现(调用方是同步线程池)。

【移植来源】
  自 chat-bi backend/app/ 忠实移植, 函数映射:
    core/agent_memory.py AgentMemoryStore        → ChatBIMemoryStore
      .save_memory/.delete_memory/.read_memory/.list_memories/
       .mark_consolidated/.get_linkage_memory/.read_index/.reconcile_index
    core/agent_memory.py get_agent_memory_store  → get_memory_store(db)
      (单例性由宿主 db 句柄承担, 不再全局缓存)
    ai/recall.py recall_memories(question, memory_dir, max_count,
      data_source_id, tenant_id)                 → recall_memories(question,
      db, max_count, user_id)
    ai/recall.py format_memories_for_prompt      → 同名(1:1, 原文保留)
    ai/recall.py extract_memory_from_turn(async) → 同名(同步化, llm 注入,
      memory_dir → db)
    ai/recall.py consolidate_memories(async)     → 同名(同步化, llm 注入)
    ai/recall.py save_query_memory(memory_dir)   → 同名(recent_queries.md 文件
      → recent_queries 记忆行, 容量控制保留)
    ai/recall.py persist_linkage_memory(mem_store, state, conv_id)
                                                 → 同名(支持 dict state;
      源"新建+竞态重查"双路径 → DB 名称幂等 upsert 单路径, 行为等价)
    ai/recall.py _get_existing_memory_summaries / _extract_join_pairs /
      _extract_join_on_conditions / _build_linkage_content /
      _extract_existing_scenes / _extract_aggregation / _clean_aggregation
                                                 → 同名私有函数(1:1)
    (chat_stream.py 调用侧"提炼→落库"组合)       → extract_and_save_memory
      (含来源对话 conversation_id 关联)
    新增管理端入口(api.py 契约)                  → recall_text / list_memories /
      delete_memory(模块级)
  适配仅限契约允许项: async→sync、llm 参数注入、存储换 PackRelationalDB、
  tenant 维度删除、get_settings→DEFAULT_* 常量(原值 memory_max_recall_count=5)。
"""
from __future__ import annotations

import json
import logging
import re
import uuid as uuid_mod
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Callable, Optional

from domains.chatbi.retrieval import parse_json_response

logger = logging.getLogger(__name__)

# ── 默认常量(原值取自 chat-bi app/core/config.py)─────────────────────
# memory_max_recall_count = 5 (最多召回 5 条, 不全量灌入)
DEFAULT_RECALL_COUNT = 5

# extract_memory_from_turn 的 name 合法性白名单 (源原文保留:
# name 是显示标题不是文件名, 允许安全字符 + 空格 + 中文)
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff\s\u3000-\u303f\uff00-\uffef]+$")

# recent_queries 流水记忆的固定 name (源 recent_queries.md 的 frontmatter name)
_RECENT_QUERIES_NAME = "recent_queries"

# ── 本模块自有 DDL(表名 chatbi_ 前缀;只含本栈的表,
#    chatbi_data_sources/chatbi_semantic_models 的建表在 models.py CHATBI_DDL,
#    由各栈 init_schema 一并幂等执行)────────────────────────────────────
CHATBI_MEMORY_DDL = [
    """CREATE TABLE IF NOT EXISTS chatbi_agent_memories (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        type TEXT NOT NULL DEFAULT 'project',
        consolidated INTEGER NOT NULL DEFAULT 0,
        co_occurrence INTEGER,
        tables_json TEXT,
        join_paths_json TEXT,
        scenes_json TEXT,
        aggregation TEXT,
        conversation_id TEXT,
        user_id TEXT,
        extra_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_memory_type ON chatbi_agent_memories(type)",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_memory_user ON chatbi_agent_memories(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_memory_name ON chatbi_agent_memories(name)",
]

# extra_metadata 中的已知结构化键(源 frontmatter metadata 的枚举字段;
# 其余键存 extra_json 列, 与源"写入 frontmatter 但 list_memories 不枚举"等价)
_KNOWN_EXTRA_KEYS = ("co_occurrence", "tables", "join_paths", "scenes",
                     "aggregation", "conversation_id")

# extra_metadata key 合法性 (源 save_memory 的防御: 避免特殊字符破坏结构)
_EXTRA_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 已完成建表的 db 实例(持强引用防 id 复用;PackRelationalDB 单例下仅登记一次)
_schema_ready: list = []


def _now() -> str:
    """ISO 时间戳(TEXT 列;与平台 Store 同惯例)。"""
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(db) -> None:
    """幂等建本栈表(每 db 实例只执行一次;init_schema 幂等, 重复无副作用)。"""
    for seen in _schema_ready:
        if seen is db:
            return
    db.init_schema(list(CHATBI_MEMORY_DDL))
    _schema_ready.append(db)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """鸭子取值:dict 键或对象属性。

    源 persist_linkage_memory/_build_linkage_content 读 AgentState 对象属性;
    目标管线(ask_data)的 state 是 dict, thinking 也是 dict——两者都兼容,
    移植函数体保持源码形态。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ── 行 → 记忆条目(源 frontmatter 解析的 DB 等价物)────────────────────

def _entry_from_row(row: dict) -> dict:
    """chatbi_agent_memories 行 → list_memories 条目(字段与源 AgentMemoryStore
    .list_memories 对齐: id/name/description/type/consolidated/created_at +
    linkage 结构化字段 co_occurrence/tables/join_paths/scenes/aggregation +
    来源对话 conversation_id)。

    与源差异(存储适配): "file"/"path" 字段随文件存储一并退役;
    另附 updated_at 与 conv_id 别名(管理端契约)。
    """
    entry: dict = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "type": row["type"] or "project",
        "consolidated": bool(row["consolidated"]),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }
    if row.get("conversation_id"):
        entry["conversation_id"] = row["conversation_id"]
        entry["conv_id"] = row["conversation_id"]  # 管理端别名
    if row.get("user_id"):
        entry["user_id"] = row["user_id"]  # 宿主归属 (tenant 目录隔离的替代维度)
    if row.get("co_occurrence") is not None:
        entry["co_occurrence"] = int(row["co_occurrence"])
    for col, key in (("tables_json", "tables"),
                     ("join_paths_json", "join_paths"),
                     ("scenes_json", "scenes")):
        raw = row.get(col)
        if raw:
            try:
                entry[key] = json.loads(raw)
            except Exception:
                entry[key] = []
    if row.get("aggregation"):
        entry["aggregation"] = row["aggregation"]
    if row.get("extra_json"):
        try:
            extra = json.loads(row["extra_json"])
            if isinstance(extra, dict):
                for k, v in extra.items():
                    entry.setdefault(k, v)
        except Exception:
            pass
    return entry


# ── ChatBIMemoryStore: 源 AgentMemoryStore 的 DB 版 ──────────────────

class ChatBIMemoryStore:
    """DB 版 Agent 记忆存储(PackRelationalDB 后端)。

    方法与源 AgentMemoryStore 一一对应:
      save_memory / list_memories / delete_memory / read_memory /
      mark_consolidated / get_linkage_memory / read_index / reconcile_index

    存储映射(源 → 本实现):
      memory/{uuid}.md 文件      → 一行记录(id=UUID 不可变主键)
      frontmatter name/description/metadata → 同名列(结构化键拆列, 其余 extra_json)
      MEMORY.md 索引             → 表本身(即索引);read_index 等价生成文本
      frontmatter 注入防御(key 白名单校验) → 保留(extra_metadata key 校验)
      更新保留原 created_at / 重写 frontmatter 重置 consolidated → 同语义保留
    """

    def __init__(self, db):
        self._db = db
        _ensure_schema(db)
        # 源 _index_reconciled: 首次 list_memories 触发一次清理
        self._reconciled = False

    # ── 写入 ─────────────────────────────────────────────────────

    def save_memory(
        self,
        name: str,
        description: str,
        content: str,
        memory_type: str = "project",
        mem_id: Optional[str] = None,
        extra_metadata: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """写入(创建或更新)一条记忆, 返回记忆 id。

        与源 save_memory 的行为对应:
          - mem_id=None → 新建(UUID 主键);mem_id=已有 id → 更新且保留 created_at
            (mem_id 不存在时按该 id 新建——源按指定文件名建文件的同语义);
          - 更新时重置 consolidated=false (源重写 frontmatter 的同款行为);
          - linkage 类型且未指定 mem_id 时按 name 幂等 upsert——同一表对只保留
            一条(源 _update_index_link 的 linkage 去重语义在行存储下的等价物);
          - extra_metadata: 已知结构化键(co_occurrence/tables/join_paths/scenes/
            aggregation/conversation_id)拆列存储;其余键存 extra_json;
            非法 key(非 ^[a-zA-Z_][a-zA-Z0-9_]*$)抛 ValueError (源同款防御)。

        Returns:
            记忆 id (源返回 Path, 行存储下主键即定位符)。
        """
        extra = dict(extra_metadata or {})
        for k in extra:
            if not _EXTRA_KEY_RE.match(str(k)):
                raise ValueError(f"Invalid extra_metadata key: {k!r}")
        # 已知结构化键拆列, 其余进 extra_json
        co_occurrence = extra.pop("co_occurrence", None)
        tables = extra.pop("tables", None)
        join_paths = extra.pop("join_paths", None)
        scenes = extra.pop("scenes", None)
        aggregation = extra.pop("aggregation", None)
        conversation_id = extra.pop("conversation_id", None)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        def _jsonify(v):
            return None if v is None else json.dumps(v, ensure_ascii=False)

        now = _now()
        with self._db.connect() as conn:
            target_id: Optional[str] = None
            created_at = now
            is_new = True
            if mem_id:
                # 更新已有记忆 (id 不可变主键);不存在则按该 id 新建(源语义)
                row = conn.execute(
                    "SELECT id, created_at FROM chatbi_agent_memories WHERE id = ?",
                    (mem_id.replace(".md", ""),),
                ).fetchone()
                if row:
                    target_id, created_at, is_new = row["id"], row["created_at"], False
                else:
                    target_id = mem_id.replace(".md", "")
            elif memory_type == "linkage":
                # linkage 同名(同表对)幂等 upsert: 保留最新一条 (源索引去重语义)
                row = conn.execute(
                    "SELECT id, created_at FROM chatbi_agent_memories "
                    "WHERE name = ? AND type = 'linkage' "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
                if row:
                    target_id, created_at, is_new = row["id"], row["created_at"], False
            if target_id is None:
                target_id = str(uuid_mod.uuid4())
            if is_new:
                conn.execute(
                    "INSERT INTO chatbi_agent_memories "
                    "(id, name, description, content, type, consolidated, "
                    " co_occurrence, tables_json, join_paths_json, scenes_json, "
                    " aggregation, conversation_id, user_id, extra_json, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_id, name, description, content, memory_type,
                     co_occurrence, _jsonify(tables), _jsonify(join_paths),
                     _jsonify(scenes), aggregation, conversation_id, user_id,
                     extra_json, created_at, now),
                )
            else:
                conn.execute(
                    "UPDATE chatbi_agent_memories SET name = ?, description = ?, "
                    "content = ?, type = ?, consolidated = 0, co_occurrence = ?, "
                    "tables_json = ?, join_paths_json = ?, scenes_json = ?, "
                    "aggregation = ?, conversation_id = ?, "
                    "user_id = COALESCE(?, user_id), extra_json = ?, "
                    "updated_at = ? WHERE id = ?",
                    (name, description, content, memory_type, co_occurrence,
                     _jsonify(tables), _jsonify(join_paths), _jsonify(scenes),
                     aggregation, conversation_id, user_id, extra_json, now,
                     target_id),
                )
        logger.info("Memory saved: %s (%s)", name, memory_type)
        return target_id

    # ── 读取 ─────────────────────────────────────────────────────

    def list_memories(self) -> list[dict]:
        """列出全部记忆条目(元数据, 不含 content 正文——源同款轻量扫描语义)。

        首次调用自动执行一次 reconcile_index (源 _index_reconciled 行为)。
        """
        if not self._reconciled:
            self._reconciled = True
            try:
                removed = self.reconcile_index()
                if removed:
                    logger.info("启动索引清理: 移除 %d 条重复条目", removed)
            except Exception as e:
                # 清理失败不阻塞 (影响: 可能残留重复 linkage 行)
                logger.warning("启动索引清理失败 (不阻塞): %s", e)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chatbi_agent_memories ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [_entry_from_row(r) for r in rows]

    def read_memory(self, mem_id: str) -> Optional[str]:
        """读取记忆正文 (源返回完整文件内容;DB 存储正文单列, 即等价物)。"""
        stem = str(mem_id).replace(".md", "")
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM chatbi_agent_memories WHERE id = ?", (stem,)
            ).fetchone()
        return row["content"] if row else None

    def delete_memory(self, mem_id: str) -> bool:
        """删除记忆。True=已删除, False=不存在 (源同款返回约定)。"""
        stem = str(mem_id).replace(".md", "")
        with self._db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM chatbi_agent_memories WHERE id = ?", (stem,))
        deleted = getattr(cur, "rowcount", 0) or 0
        if deleted:
            logger.info("Memory deleted: %s", stem)
        return deleted > 0

    def mark_consolidated(self, mem_id: str) -> bool:
        """标记为已整理 (整理后默认隐藏, recall 跳过;原记忆不删除, 可追溯)。"""
        stem = str(mem_id).replace(".md", "")
        with self._db.connect() as conn:
            cur = conn.execute(
                "UPDATE chatbi_agent_memories SET consolidated = 1 WHERE id = ?",
                (stem,))
        return (getattr(cur, "rowcount", 0) or 0) > 0

    def get_linkage_memory(self, table_a: str, table_b: str) -> Optional[dict]:
        """按表对查询 linkage 记忆 (表对字典序规范化, 支持 caller 乱序传入)。"""
        pair = sorted([table_a, table_b])
        for m in self.list_memories():
            if m.get("type") != "linkage":
                continue
            tables = m.get("tables")
            if tables and sorted(tables) == pair:
                return m
        return None

    # ── 索引等价物 ───────────────────────────────────────────────

    def read_index(self) -> str:
        """生成 MEMORY.md 等价的索引文本 (行存储下由数据即时合成)。

        截断保护与源一致: 最大 200 行或 25KB。
        """
        lines = [
            "# Agent Memory Index",
            "",
            "This directory stores persistent agent memories.",
            "Each entry below links to a memory file.",
            "",
            "---",
            "",
        ]
        for m in self.list_memories():
            lines.append(f"- [{m['id']}]({m['id']}) — {m['description']}")
        content = "\n".join(lines)
        parts = content.split("\n")
        if len(parts) > 200:
            content = "\n".join(parts[:200])
        if len(content.encode("utf-8")) > 25_000:
            content = content[:25_000]
        return content

    def reconcile_index(self) -> int:
        """索引一致性防御: 同名 linkage 记忆去重 (同一表对只保留最新一条)。

        源 reconcile_index 清理"幽灵索引条目"(索引指向已删除文件)——行存储下
        表即索引、无幽灵条目问题, 该职责消失;linkage 去重职责保留 (防御
        历史数据/并发窗口产生的重复表对行)。返回清理条数。
        """
        removed = 0
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM chatbi_agent_memories "
                "WHERE type = 'linkage' ORDER BY updated_at ASC, id ASC"
            ).fetchall()
            keep: dict[str, str] = {}
            for r in rows:
                keep[r["name"]] = r["id"]  # ASC 遍历, 后者(最新)覆盖
            for r in rows:
                if keep.get(r["name"]) != r["id"]:
                    conn.execute(
                        "DELETE FROM chatbi_agent_memories WHERE id = ?",
                        (r["id"],))
                    removed += 1
        if removed:
            logger.info("索引清理完成: 移除 %d 条重复 linkage 条目", removed)
        return removed


def get_memory_store(db) -> ChatBIMemoryStore:
    """获取记忆存储实例 (源 get_agent_memory_store 的对应物)。

    源为进程级单例(base_dir 首次生效);目标依赖以 db 句柄注入, 单例性由
    宿主的 db 单例(runtime.get_pack_db)承担, 本工厂每调用即建轻量壳。
    """
    return ChatBIMemoryStore(db)


# ── 相关性召回 (源 recall.recall_memories / format_memories_for_prompt) ──

def _tokenize(text: str) -> set[str]:
    """分词: 英文按 \\b 词元 + 中文按单字, 全部小写 (源 _tokenize 原文移植)。"""
    tokens: set[str] = set()
    for m in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.add(m.group().lower())
    for m in re.finditer(r"[\u4e00-\u9fff]", text):
        tokens.add(m.group())
    return tokens


def recall_memories(
    question: str,
    db,
    max_count: Optional[int] = None,
    user_id: Optional[str] = None,
) -> list[dict]:
    """按相关性召回记忆 (最多 max_count 条)。

    对标 Claude Code §5.4: 不全量灌入, 只召回相关的。
    策略: 记忆 description+name 与问题的关键词重叠度 (轻量, 不调 LLM)。

    Args:
        question: 用户问题
        db: pack 关系库 (源 memory_dir 的存储等价物)
        max_count: 最多返回条数 (None → DEFAULT_RECALL_COUNT=5,
            即 chat-bi memory_max_recall_count)
        user_id: 宿主用户过滤 (源 tenant+data_source 目录隔离的替代维度;
            None = 全量)

    Returns:
        [{id, name, description, content}, ...] 按相关性降序, 无匹配返回空。
    """
    if max_count is None:
        max_count = DEFAULT_RECALL_COUNT
    _ensure_schema(db)

    # 候选行: 跳过已整理(consolidated)与 linkage(结构化数据, 不适合注入
    # prompt; 通过图谱 confidence 间接影响 SQL 生成)——源按文件头正则跳过,
    # 行存储下以 WHERE 等价表达
    sql = ("SELECT id, name, description, content FROM chatbi_agent_memories "
           "WHERE consolidated = 0 AND type != 'linkage'")
    params: list = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    with db.connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        return []

    # 关键词相关性排序 (description + name 与问题的词重叠)
    # 不用 LLM 排序: 记忆数量少, 简单重叠度即可, 零 API 成本 (源同款取舍)
    question_words = _tokenize(question)
    scored: list[tuple[int, dict]] = []
    for row in rows:
        mem = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "content": row["content"] or "",
        }
        mem_words = _tokenize(mem["name"] + " " + mem["description"])
        overlap = len(question_words & mem_words)
        if overlap > 0:
            scored.append((overlap, mem))

    if not scored:
        return []  # 无相关记忆, 宁缺毋滥

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:max_count]]


def format_memories_for_prompt(memories: list[dict]) -> str:
    """格式化记忆为 prompt 片段 (注入 SQL 生成动态段;源 1:1, 原文保留)。"""
    if not memories:
        return ""

    lines = ["【Agent 记忆 (相关业务知识)】"]
    for mem in memories:
        lines.append(f"[{mem['name']}] {mem['description']}")
        content = mem.get("content", "")
        # 剥 frontmatter (源为文件存储设计;DB 正文无 frontmatter, 该步为
        # 兼容调用方直传文件风格内容而保留, 对纯正文是空操作)
        body = re.sub(r"^---\n.*?\n---\n?", "", content, flags=re.DOTALL).strip()
        if body:
            # 截断前 200 字符: 防止单条记忆过长导致 prompt 膨胀
            lines.append(body[:200])
    return "\n".join(lines)


def recall_text(
    llm,
    db,
    question: str,
    conv_id: Optional[str] = None,
    user_id: Optional[str] = None,
    top_k: int = DEFAULT_RECALL_COUNT,
) -> str:
    """召回相关记忆并格式化为注入文本 (api/tools 契约入口)。

    = 源调用侧 `recall_memories(...) + format_memories_for_prompt(...)` 的
    组合(见 chat.py _generate_sql_with_fewshot), 内化其 try/except 降级:
      - 无相关记忆 → "" (宁缺毋滥, 调用方跳过注入段);
      - 存储异常 → "" (fail-open, 源调用侧 logger.debug 后无记忆继续)。

    Args:
        llm: 引擎 LLMClient——注入约定保留;召回路径零 LLM 调用 (源设计
            "省调用", 关键词重叠排序), 参数不使用。
        db: pack 关系库
        question: 用户问题
        conv_id: 会话 id (无 LLM 调用, 仅为统一注入约定保留)
        user_id: 宿主用户过滤 (可选)
        top_k: 最多召回条数 (默认 5 = 源 memory_max_recall_count)

    Returns:
        格式化记忆文本;无记忆/失败返回 ""。
    """
    try:
        memories = recall_memories(question, db, max_count=top_k, user_id=user_id)
        return format_memories_for_prompt(memories)
    except Exception as e:
        logger.warning("recall_text 记忆召回失败, 降级无记忆: %s", e)
        return ""


# ── 管理端入口 (api.py /memories 端点契约;对应源 api/memory.py)──────────

def list_memories(
    db,
    user_id: Optional[str] = None,
    limit: int = 50,
    include_consolidated: bool = True,
) -> list[dict]:
    """管理端记忆列表 (含来源对话 conversation_id/conv_id 与 created_at)。

    对应源 GET /memory 端点 (api/memory.py list_memories + MemoryOut 字段):
    id/name/description/type/content/consolidated/created_at/conversation_id +
    linkage 结构化字段。差异说明: 源默认隐藏 consolidated(查看语义), 管理端
    需要全量可见才能管理, 故 include_consolidated 默认 True, 传 False 复现
    源默认;排序 newest-first(源为目录 glob 序, 管理列表取自然语义)。

    Args:
        db: pack 关系库
        user_id: 宿主用户过滤 (None = 全量)
        limit: 返回条数上限 (默认 50)
        include_consolidated: 是否包含已整理记忆

    Returns:
        记忆条目列表 (含 content 正文)。
    """
    _ensure_schema(db)
    sql = "SELECT * FROM chatbi_agent_memories"
    where = []
    params: list = []
    if not include_consolidated:
        where.append("consolidated = 0")
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    entries = [_entry_from_row(r) for r in rows]
    for entry, row in zip(entries, rows):
        entry["content"] = row.get("content") or ""
    return entries


def delete_memory(db, memory_id: str) -> bool:
    """删除记忆 (管理端契约;True=已删除, False=不存在)。"""
    return get_memory_store(db).delete_memory(memory_id)


# ── LLM 自主提炼 (源 recall.extract_memory_from_turn + 调用侧组合)────────

def _get_existing_memory_summaries(store: ChatBIMemoryStore) -> str:
    """获取已有记忆的摘要 (供 LLM 避免重复;源同名私有函数 1:1)。

    数据流: 读取全部记忆条目的 name/description 拼接摘要文本传给 LLM,
    作为"去重"的第一道防线 (第二道在 consolidate_memories 中合并)。
    """
    summaries = []
    for m in store.list_memories():
        summaries.append(f"- {m['name']}: {m.get('description', '')}")
    return "\n".join(summaries) if summaries else "(无)"


def extract_memory_from_turn(
    llm,
    question: str,
    sql: str,
    tables: list[str],
    reply: str,
    db,
    conv_id: Optional[str] = None,
) -> Optional[dict]:
    """LLM 自主提炼 — 判断本轮对话是否产生了值得跨会话保留的新知识。

    对标 Claude Code: Agent 在对话中自动识别值得记住的事实和偏好。
    只提取"业务约定/字段含义/用户偏好"类知识, 不记录一次性查询结果。
    prompt 与源原文一致 (含已有记忆摘要去重段)。

    Args:
        llm: 引擎 LLMClient (.chat_json 返回 dict;源 llm_chat +
            parse_json_response 的等价注入)
        question: 用户问题
        sql: 生成的 SQL
        tables: 涉及的表名
        reply: Agent 回复
        db: pack 关系库 (源 memory_dir 的等价物, 读已有记忆做去重)
        conv_id: 会话 id (LLM 调用日志关联)

    Returns:
        提炼结果 {name, description, type, content} 或 None (不值得记/失败)。
    """
    # 读取已有记忆, 避免重复 (源同款第一道去重防线)
    existing_summaries = _get_existing_memory_summaries(get_memory_store(db))

    # prompt 与源原文一致
    prompt = (
        "你是 BI Agent 的记忆提炼模块。判断本轮对话是否产生了值得跨会话保留的新知识。\n\n"
        "【值得记的】\n"
        "- 新发现的业务约定 (如 status=2 表示审核中)\n"
        "- 字段含义的澄清 (如 avg_rating 是所有评价的均值, 不是中位数)\n"
        "- 用户表达的偏好 (如 查占比时用百分比格式)\n"
        "- 修正了之前的错误理解\n\n"
        "【不值得记的】\n"
        "- 一次性的查询结果 (如 本月销售额 120 万)\n"
        "- 与已有记忆重复的知识\n"
        "- 纯技术细节 (SQL 优化建议)\n"
        "- 用户临时性的表述 (如 换个图表)\n\n"
        f"【已有记忆 (避免重复)】\n{existing_summaries}\n\n"
        f"【本轮对话】\n"
        f"用户问: {question}\n"
        f"生成 SQL: {sql[:300]}\n"
        f"涉及表: {', '.join(tables[:5])}\n"
        f"Agent 回复: {reply[:200]}\n\n"
        "判断: 如果有值得记的新知识, 返回 JSON (严格格式):\n"
        '{"should_save": true, "name": "英文slug", "description": "一句话描述(用于关键词匹配召回)", '
        '"type": "project", "content": "记忆正文(Markdown)"}\n\n'
        "如果无新知识值得记, 返回:\n"
        '{"should_save": false}\n\n'
        "只返回 JSON, 不要解释。"
    )

    try:
        parsed = llm.chat_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            stage="chatbi.memory.extract",
            conv_id=conv_id,
        )
    except Exception as e:
        # LLM 挂 → 不阻断主流程 (源 except 分支同款 fail-open)
        logger.warning("extract_memory_from_turn 失败 (不阻塞): %s", e)
        return None
    if not parsed or not isinstance(parsed, dict):
        return None
    if not parsed.get("should_save"):
        return None
    # 校验必填字段
    name = (parsed.get("name") or "").strip()
    desc = (parsed.get("description") or "").strip()
    mem_content = (parsed.get("content") or "").strip()
    if not name or not mem_content:
        return None
    # name 安全检查: 允许安全字符 + 空格 + 中文 (name 是显示标题不是文件名)
    if not _SAFE_NAME_RE.match(name):
        logger.warning("extract_memory: LLM 生成的 name 不合法: %s", name)
        return None
    return {
        "name": name,
        "description": desc,
        "type": parsed.get("type", "project"),
        "content": mem_content,
    }


def extract_and_save_memory(
    llm,
    db,
    question: str,
    sql: str,
    tables: list[str],
    reply: str,
    conv_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[dict]:
    """提炼并落库 — 源 chat_stream.py 调用侧组合的完整闭环移植。

    源流程 (chat_stream.py 成功查询后):
      extracted = await extract_memory_from_turn(...)
      if extracted: mem_store.save_memory(..., extra_metadata={"conversation_id": conv_id})

    Returns:
        落库结果 (extracted + id) 或 None (无新知识/失败;失败不阻断主流程)。
    """
    extracted = extract_memory_from_turn(
        llm, question, sql, tables, reply, db, conv_id=conv_id)
    if not extracted:
        return None
    mem_id = get_memory_store(db).save_memory(
        name=extracted["name"],
        description=extracted["description"],
        content=extracted["content"],
        memory_type=extracted.get("type", "project"),
        extra_metadata=({"conversation_id": conv_id} if conv_id else None),
        user_id=user_id,
    )
    extracted["id"] = mem_id
    logger.info("LLM 自主提炼记忆: %s", extracted["name"])
    return extracted


# ── 记忆整理 (源 recall.consolidate_memories, async → sync)───────────────

def consolidate_memories(
    llm,
    db,
    ids: Optional[list[str]] = None,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """整理记忆 — LLM 合并去重碎片化记忆。

    当记忆条目较多时, 将碎片化的记忆合并为更精炼的几条。
    原有记忆标记 consolidated=true (默认隐藏), 合并结果作为新记忆写入。

    触发时机: 通常由定时任务或手动触发, 非每轮对话自动执行。
    设计原则 (源原文):
      - 合并后不删除原始记忆 (只标记隐藏), 可追溯
      - linkage 类型是结构化数据, 跳过 (LLM 整理会破坏结构)
      - 单条或少条时跳过 (不值得调 LLM)

    Args:
        llm: 引擎 LLMClient (.chat 返回 (content, meta);源 llm_chat +
            parse_json_response 的等价注入——整理结果是 JSON 数组,
            走 chat+容错解析而非 chat_json)
        db: pack 关系库
        ids: 只整理指定的记忆 (None = 整理全部未整理的)
        on_progress: 进度回调 (progress 0-100, stage 描述文字)

    Returns:
        {"consolidated": int, "total": int, "detail": str}
    """
    def _progress(pct: int, stage: str):
        if on_progress:
            on_progress(pct, stage)

    _progress(5, "准备中...")

    store = get_memory_store(db)
    all_memories = store.list_memories()
    # 过滤: 已整理的不参与, ids 非空时只取指定的 (空列表/None 都表示整理全部)
    memories = []
    for m in all_memories:
        if m.get("consolidated"):
            continue
        if m.get("type") == "linkage":
            continue
        if ids and m["id"] not in ids:
            continue
        memories.append(m)

    if len(memories) <= 1:
        _progress(100, "完成")
        return {"consolidated": 0, "total": len(memories), "detail": "记忆条目较少, 无需整理"}

    # 读取所有记忆内容 (DB 正文即 Markdown body;源在此剥离 frontmatter,
    # 该步骤随文件存储一并退役)
    _progress(10, "读取记忆内容...")
    all_content = []
    for m in memories:
        body = store.read_memory(m["id"]) or ""
        if body:
            all_content.append(
                f"[{m['name']}] ({m.get('type', 'project')}) {m.get('description', '')}\n{body}")

    if not all_content:
        _progress(100, "完成")
        return {"consolidated": 0, "total": len(memories), "detail": "无有效记忆内容可整理"}

    _progress(30, "调用 LLM 整理中...")

    # prompt 与源原文一致
    prompt = (
        "你是 BI Agent 的记忆整理模块。将以下碎片化的记忆合并为更精炼的几条。\n\n"
        "规则:\n"
        "- 合并重复或高度相关的记忆\n"
        "- 保留所有有价值的知识, 不要丢失信息\n"
        "- 每条记忆有明确独立的主题\n"
        "- 返回 JSON 数组, 每项格式:\n"
        '  {"name": "简短标题", "description": "一句话描述", "type": "project", "content": "Markdown正文"}\n\n'
        f"【现有记忆 ({len(all_content)} 条)】\n"
        + "\n---\n".join(all_content)
        + "\n\n只返回 JSON 数组, 不要解释。"
    )

    try:
        content, _ = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            stage="chatbi.memory.consolidate",
        )

        _progress(80, "写入整理结果...")
        parsed = parse_json_response(content)
        if not parsed:
            _progress(100, "完成")
            return {"consolidated": 0, "total": len(memories), "detail": "LLM 未返回有效结果"}
        # 兼容: LLM 可能返回 dict 而非 list
        if isinstance(parsed, dict):
            parsed = parsed.get("memories", [parsed])
        if not isinstance(parsed, list):
            _progress(100, "完成")
            return {"consolidated": 0, "total": len(memories), "detail": "LLM 返回格式异常"}

        # 写入整理后的记忆 (每条生成新 id, 类型统一为 consolidated)
        saved = 0
        for item in parsed:
            name = (item.get("name") or "").strip()
            desc = (item.get("description") or "").strip()
            mem_content = (item.get("content") or "").strip()
            if not name or not mem_content:
                continue
            store.save_memory(
                name=name,
                description=desc or name,
                content=mem_content,
                memory_type="consolidated",
            )
            saved += 1

        # 标记原始记忆为已整理
        _progress(90, "标记原始记忆...")
        marked = 0
        for m in memories:
            store.mark_consolidated(m["id"])
            marked += 1

        _progress(100, "完成")
        return {
            "consolidated": saved,
            "total": len(memories),
            "detail": f"整理为 {saved} 条精炼记忆, {marked} 条原始记忆已标记为已整理 (默认隐藏)",
        }
    except Exception as e:
        logger.warning("consolidate_memories 失败: %s", e)
        return {"consolidated": 0, "total": len(memories), "detail": f"整理失败: {e}"}


# ── 查询流水 (源 recall.save_query_memory, 无生产调用方, 保留备用)────────

def save_query_memory(
    question: str,
    tables: list[str],
    db,
    user_id: Optional[str] = None,
) -> None:
    """记录用户查询到 recent_queries 记忆 (MEM-01 自主记忆写入闭环)。

    源为无生产调用方的备用函数, 1:1 保留。存储映射:
      recent_queries.md 文件(2 行 frontmatter + 追加行) → 一条
      name="recent_queries" 的记忆行(content 为追加行列表);
      容量控制(超 200 行截断, 保留 frontmatter + 后 100 行) → 行存储下
      保留后 100 行(frontmatter 随文件格式退役)。

    边界情况 (源同款):
      - question 为空时直接返回 (不写入空记录)
      - 失败不阻塞 (logger.debug)
    """
    if not question:
        return
    try:
        store = get_memory_store(db)
        ts = datetime.now().strftime("%m-%d %H:%M")
        tables_str = ", ".join(tables) if tables else "-"
        entry_line = f"- [{ts}] {question} (表: {tables_str})"
        existing = None
        for m in store.list_memories():
            if m["name"] == _RECENT_QUERIES_NAME:
                existing = m
                break
        if existing:
            body = store.read_memory(existing["id"]) or ""
            lines = body.split("\n") if body else []
            lines.append(entry_line)
            if len(lines) > 200:
                lines = lines[-100:]  # 容量控制: 保留最近 100 条
            store.save_memory(
                name=_RECENT_QUERIES_NAME,
                description="用户最近的查询记录和常用表",
                content="\n".join(lines),
                memory_type="project",
                mem_id=existing["id"],
                user_id=user_id,
            )
        else:
            store.save_memory(
                name=_RECENT_QUERIES_NAME,
                description="用户最近的查询记录和常用表",
                content=entry_line,
                memory_type="project",
                user_id=user_id,
            )
    except Exception as e:
        logger.debug("save_query_memory 失败 (不阻塞): %s", e)


# ── 链路经验沉淀 (源 recall.persist_linkage_memory, E1 Task 2.1)──────────

def _extract_join_pairs(join_path_section: str) -> set[tuple[str, str]]:
    """从 join_path_section 提取直接 JOIN 的表对（字典序）。

    用于判断某个表对是否有直接 JOIN 路径（W2: 非直接关联标注）。
    join_path_section 格式类似: "biz_orders.user_id = biz_users.id\\n..."
    (源 1:1 移植)
    """
    if not join_path_section:
        return set()
    pairs: set[tuple[str, str]] = set()
    # 匹配 table.column = table.column 模式
    for m in re.finditer(r"([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*\s*=\s*([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*", join_path_section):
        pairs.add(tuple(sorted([m.group(1), m.group(2)])))
    return pairs


def _extract_join_on_conditions(
    join_path_section: str, table_a: str, table_b: str
) -> list[dict[str, str]]:
    """从 join_path_section 提取特定表对的直接 ON 条件 (结构化;源 1:1)。

    只提取 table_a 和 table_b **直接 JOIN** 的 ON 条件,
    跳过通过中间表间接关联的路径段。

    Returns:
        [{"on": "biz_orders.user_id = biz_users.id", "join_type": "LEFT"}, ...]
    """
    if not join_path_section:
        return []
    results: list[dict[str, str]] = []
    for line in join_path_section.split("\n"):
        line = line.strip()
        if not line:
            continue
        if table_a not in line or table_b not in line:
            continue

        # 判断是否为多跳格式 (含 JOIN 关键字)
        has_join = bool(re.search(r"\b(?:LEFT|RIGHT|INNER|CROSS)?\s*JOIN\b", line, re.IGNORECASE))

        if has_join:
            # 多跳格式: 按 JOIN 拆成独立段, 每段格式: "table_b ON condition (confidence=x)"
            segments = re.split(r"\b(?:LEFT|RIGHT|INNER|CROSS)?\s*JOIN\b", line, flags=re.IGNORECASE)
            for seg in segments[1:]:  # 跳过第一段 (FROM 表, 不是 JOIN)
                # 提取 ON 子句
                on_match = re.search(r"\bON\s+(.+?)(?:\s+\(confidence|$)", seg, re.IGNORECASE)
                if not on_match:
                    continue
                on_clause = on_match.group(1).strip()
                on_clause = re.sub(r"\s*\(confidence.*$", "", on_clause).strip()
                # 检查 ON 条件是否直接关联 table_a 和 table_b
                col_match = re.search(
                    r"([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*\s*=\s*([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*",
                    on_clause,
                )
                if col_match and {col_match.group(1), col_match.group(2)} == {table_a, table_b}:
                    results.append({"on": on_clause, "join_type": "LEFT"})
        else:
            # 简单格式: "table_a.col = table_b.col" — 直接提取等值条件
            col_match = re.search(
                r"([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*\s*=\s*([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*",
                line,
            )
            if col_match and {col_match.group(1), col_match.group(2)} == {table_a, table_b}:
                on_clause = col_match.group(0)
                # 去掉可能的 confidence 标注
                on_clause = re.sub(r"\s*\(confidence.*$", "", on_clause).strip()
                results.append({"on": on_clause, "join_type": "LEFT"})
    return results


def _build_linkage_content(
    table_a: str,
    table_b: str,
    state,
    direct_join_pairs: set[tuple[str, str]],
    existing_scenes: Optional[list[str]] = None,
) -> str:
    """构建 linkage 记忆的 content (Markdown body, 给 LLM 看;源 1:1)。

    Args:
        direct_join_pairs: 本轮查询中有直接 JOIN 的表对集合（判断间接关联）
        existing_scenes: 已记录的场景列表（更新时追加新场景，去重）
    """
    content_parts = []
    pair = tuple(sorted([table_a, table_b]))
    is_direct = pair in direct_join_pairs
    join_path_section = _attr(state, "join_path_section") or ""
    current_tables = _attr(state, "current_tables") or []
    question = _attr(state, "user_input") or _attr(state, "question")

    # JOIN 路径
    if is_direct and join_path_section:
        # 直接关联：提取该表对的 JOIN 行（而非整段）
        join_lines = []
        for line in join_path_section.split("\n"):
            if table_a in line and table_b in line:
                join_lines.append(line.strip())
        if join_lines:
            content_parts.append("## JOIN 路径\n" + "\n".join(join_lines) + "\n")
    elif not is_direct:
        # 间接关联：标注经由哪些表
        intermediaries = []
        for t in current_tables:
            if t in (table_a, table_b):
                continue
            if tuple(sorted([table_a, t])) in direct_join_pairs and tuple(sorted([table_b, t])) in direct_join_pairs:
                intermediaries.append(t)
        via = "、".join(intermediaries) if intermediaries else "其他表"
        content_parts.append(f"## 关联方式\n间接关联（经由 {via}）\n")

    # 典型场景（追加模式：去重）
    scenes = list(existing_scenes) if existing_scenes else []
    if question and question not in scenes:
        scenes.append(question)
    if scenes:
        content_parts.append("## 典型场景\n" + "\n".join(f"- {s}" for s in scenes) + "\n")

    # 聚合方式
    thinking = _attr(state, "thinking")
    agg_text = _attr(thinking, "aggregation") if thinking is not None else None
    if agg_text:
        # 只保留聚合关键词, 不存 LLM 原始长文本
        agg_clean = _clean_aggregation(str(agg_text))
        if agg_clean:
            content_parts.append(f"## 聚合方式\n{agg_clean}\n")

    return "\n".join(content_parts)


def _extract_existing_scenes(content: str, frontmatter_scenes: Optional[list[str]] = None) -> list[str]:
    """从已有 linkage 记忆提取已记录的场景列表（去重用;源 1:1）。

    优先从 frontmatter_scenes (结构化字段) 读取，回退到 Markdown body 解析。
    """
    if frontmatter_scenes:
        return list(frontmatter_scenes)
    if not content:
        return []
    scenes: list[str] = []
    in_scenes = False
    for line in content.split("\n"):
        if line.startswith("## 典型场景"):
            in_scenes = True
            continue
        if line.startswith("## "):
            in_scenes = False
            continue
        if in_scenes and line.strip().startswith("- "):
            scenes.append(line.strip()[2:])
    return scenes


def _extract_aggregation(state) -> str:
    """从 state.thinking 提取聚合方式 (简洁关键字;源 1:1)。

    LLM 返回的 aggregation 可能是长文本如 "SUM(actual_amount) 按 category 分组",
    只提取首个聚合函数名 (SUM/COUNT/AVG/MAX/MIN) 作为存储值。
    """
    thinking = _attr(state, "thinking")
    if thinking:
        agg = _attr(thinking, "aggregation")
        if agg:
            return _clean_aggregation(str(agg))
    return ""


def _clean_aggregation(agg_str: str) -> str:
    """清洗 aggregation 值: 提取聚合关键字, 截断过长文本 (源 1:1)。"""
    m = re.search(r"\b(SUM|COUNT|AVG|MAX|MIN)\b", agg_str, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # 无匹配则截取前 20 字符 (兜底)
    return agg_str[:20].strip()


def _merge_and_save_linkage(
    store: ChatBIMemoryStore,
    table_a: str,
    table_b: str,
    state,
    direct_join_pairs: set[tuple[str, str]],
    join_paths: list[dict[str, str]],
    aggregation: str,
    existing: dict,
    conv_id: Optional[str],
    user_id: Optional[str],
) -> None:
    """更新已存在的 linkage 记忆 (源"更新分支 + 竞态重查分支"的合并实现)。

    源因文件系统竞态把同一段更新逻辑写了两遍;行存储下两分支完全同构,
    收敛为一个函数 (行为等价): co_occurrence+1、JOIN 路径清洗合并、
    场景去重追加、aggregation 新值优先。
    """
    table_pair = (table_a, table_b)
    new_co = existing["co_occurrence"] + 1

    # 从结构化字段读取已有 scenes (优先), 回退到 Markdown body (源同款)
    existing_fm_scenes = existing.get("scenes")
    original_content = store.read_memory(existing["id"]) or ""
    existing_scenes = _extract_existing_scenes(original_content, existing_fm_scenes)

    # 合并 JOIN 路径: 保留已有 + 追加新的 (去重)
    # 清洗已有的脏 on 值 (旧数据可能含 "LEFT JOIN" 或 "confidence")
    existing_join_paths = existing.get("join_paths") or []
    cleaned_existing: list[dict[str, str]] = []
    for jp in existing_join_paths:
        on_val = jp.get("on", "")
        # 脏数据: on 值包含 LEFT JOIN (多跳路径整行被写入)
        if "LEFT JOIN" in on_val.upper() or "JOIN" in on_val.upper().split()[0:1]:
            col_match = re.search(
                r"([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*\s*=\s*([a-zA-Z_][\w]*)\.[a-zA-Z_][\w]*",
                on_val,
            )
            if col_match and {col_match.group(1), col_match.group(2)} == {table_a, table_b}:
                cleaned_existing.append({"on": col_match.group(0), "join_type": jp.get("join_type", "LEFT")})
            # 否则丢弃脏数据
        elif "confidence" in on_val.lower():
            # 去掉 confidence 标注
            clean_on = re.sub(r"\s*\(confidence.*$", "", on_val).strip()
            cleaned_existing.append({"on": clean_on, "join_type": jp.get("join_type", "LEFT")})
        else:
            cleaned_existing.append(jp)
    existing_join_paths = cleaned_existing
    existing_on_set = {jp.get("on", "") for jp in existing_join_paths}
    for jp in join_paths:
        if jp.get("on", "") not in existing_on_set:
            existing_join_paths.append(jp)
            existing_on_set.add(jp.get("on", ""))
    merged_join_paths = existing_join_paths

    # 重建 content（含追加新场景 + 保留 JOIN 路径等）
    new_content = _build_linkage_content(
        table_a, table_b, state, direct_join_pairs, existing_scenes
    )

    # 合并 scenes: 已有 + 新问题 (去重)
    question = _attr(state, "user_input") or _attr(state, "question")
    merged_scenes = list(existing_scenes)
    if question and question not in merged_scenes:
        merged_scenes.append(question)

    # 合并 aggregation: 优先用新值, 否则清洗旧值
    existing_agg = existing.get("aggregation", "")
    final_aggregation = aggregation or (existing_agg if existing_agg else "")
    if final_aggregation and len(final_aggregation) > 10:
        final_aggregation = _clean_aggregation(final_aggregation)

    store.save_memory(
        name=f"linkage-{table_a}-{table_b}",
        description=f"表 {table_a} 和 {table_b} 的共现经验",
        content=new_content,
        memory_type="linkage",
        mem_id=existing["id"],
        extra_metadata={
            "co_occurrence": new_co,
            "tables": sorted(table_pair),
            "join_paths": merged_join_paths,
            "scenes": merged_scenes,
            **({"aggregation": final_aggregation} if final_aggregation else {}),
            **({"conversation_id": conv_id} if conv_id else {}),
        },
        user_id=user_id,
    )
    logger.info("更新 linkage 记忆 %s-%s: co_occurrence=%s", table_a, table_b, new_co)


def persist_linkage_memory(
    db_or_store,
    state,
    conv_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """沉淀链路经验到 linkage 记忆 (E1 Task 2.1;源 1:1)。

    从 state 直接读取表对（不解析 SQL），对每对表创建或更新 linkage 记忆。
    单表查询跳过（无 JOIN 信号）。

    结构化存储字段 (程序化消费;graph_infer.sync_linkage_to_graph 读取):
      - co_occurrence: 共现次数
      - tables: [表 A, 表 B] (字典序)
      - join_paths: [{"on": "...", "join_type": "LEFT"}, ...]
      - scenes: ["本月各品类销售额", ...]
      - aggregation: "SUM" (标量字符串)

    Markdown body 保留 (给 LLM 注入 prompt 用)。

    Args:
        db_or_store: pack 关系库或 ChatBIMemoryStore (源 mem_store 参数;
          鸭子判断, graph_infer 侧传 store, 管线侧传 db 均可)
        state: 查询状态——源 AgentState 对象或目标管线 dict, 需含
          current_tables / join_path_section / question / thinking.aggregation
        conv_id: 对话 ID (追溯来源)
        user_id: 宿主用户 (可选归属)
    """
    # 鸭子判断: 有 save_memory 即视为 store, 否则视为 db
    if hasattr(db_or_store, "save_memory"):
        store = db_or_store
    else:
        store = get_memory_store(db_or_store)

    current_tables = _attr(state, "current_tables") or []
    # 单表查询跳过
    if len(current_tables) < 2:
        return

    # 预计算直接 JOIN 表对集合（W2: 判断间接关联）
    join_path_section = _attr(state, "join_path_section") or ""
    direct_join_pairs = _extract_join_pairs(join_path_section)

    # 提取聚合方式
    aggregation = _extract_aggregation(state)

    # 生成所有表对组合
    table_pairs = list(combinations(sorted(current_tables), 2))

    for table_a, table_b in table_pairs:
        # 提取该表对的 JOIN ON 条件 (结构化)
        join_paths = _extract_join_on_conditions(join_path_section, table_a, table_b)

        # 检查是否已有该表对的 linkage 记忆
        existing = store.get_linkage_memory(table_a, table_b)

        if existing:
            # 已存在：co_occurrence + 1，追加新场景（W1: 若 question 新颖）
            _merge_and_save_linkage(
                store, table_a, table_b, state, direct_join_pairs,
                join_paths, aggregation, existing, conv_id, user_id)
        else:
            # 新表对：创建 linkage 记忆
            # 防竞态: 写入前再次检查 (源同款;DB 名称幂等 upsert 兜底,
            # 竞态命中即走更新路径, 不会重复建行/丢增量)
            recheck = store.get_linkage_memory(table_a, table_b)
            if recheck:
                logger.warning("竞态检测: %s-%s 已存在, 走更新路径", table_a, table_b)
                _merge_and_save_linkage(
                    store, table_a, table_b, state, direct_join_pairs,
                    join_paths, aggregation, recheck, conv_id, user_id)
            else:
                content = _build_linkage_content(table_a, table_b, state, direct_join_pairs)
                question = _attr(state, "user_input") or _attr(state, "question")
                scenes = [question] if question else []
                store.save_memory(
                    name=f"linkage-{table_a}-{table_b}",
                    description=f"表 {table_a} 和 {table_b} 的共现经验",
                    content=content,
                    memory_type="linkage",
                    extra_metadata={
                        "co_occurrence": 1,
                        "tables": sorted([table_a, table_b]),
                        "join_paths": join_paths,
                        "scenes": scenes,
                        **({"aggregation": aggregation} if aggregation else {}),
                        **({"conversation_id": conv_id} if conv_id else {}),
                    },
                    user_id=user_id,
                )
                logger.info("创建 linkage 记忆 %s-%s", table_a, table_b)
