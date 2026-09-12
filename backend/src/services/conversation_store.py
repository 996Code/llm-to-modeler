"""ConversationStore - PostgreSQL 会话持久化(append-only 事件流架构)。

【模块定位】
本模块是整个会话系统的"数据库访问层"(DAO/Repository),负责把对话、消息、
工具产出、调用日志等数据经 psycopg 连接池持久化到 PG。可以理解成 Java 项目里的
``@Repository`` + ``DataSource`` + MyBatis Mapper 的合体:既管连接、也管 SQL。

【核心设计 - 事件溯源 (Event Sourcing)】
阶段 4 把传统的"会话表 + 消息表"两表模型,重构为 append-only 的事件流模型:
  - 旧 conversations / messages 表 RENAME 为 ``_legacy_*`` 留档(只保留不迁移数据)
  - 新建 events 表(只追加,不修改)—— 类似 Kafka 的 topic
  - 新建 session_meta 表(会话元数据,供列表查询,避免扫整张 events 表)

为什么用 append-only?类比 Java 的事件溯源 / Kafka:
  - 历史消息天然不可变,只 INSERT 不 UPDATE,天然审计友好
  - 新需求(压缩点、追问现场、artifact 快照)只是新增 kind,不动表结构
  - 列表查询性能好:session_meta 是物化的"读模型"(CQRS 思想)

【三张核心表】
  1. events            —— append-only 事件流,所有对话内容都在这里
  2. session_meta      —— 会话元数据(标题/配置/时间戳),供 list 查询
  3. call_logs         —— LLM / 上游服务的调用审计日志(排查问题用)

events.kind 取值(类比 Java 的枚举):
  - user          用户输入
  - assistant     助手回复
  - tool_result   工具产出(经过 summary 标准化)
  - compacted     压缩点标记(历史压缩的锚点)
  - compact_trace 压缩轨迹(审计用)
  - checkpoint    artifact 快照 / 会话生命周期标记
  - ask           pending_ask 现场(追问未回答时的现场快照)
  - trace         链路追踪打点(管理端链路视图;引擎自动打点 +
                  pack 经 ToolContext.trace() 写入的业务打点。
                  conversation.load 的重放分流忽略本 kind,不进消息上下文)

【兼容性】
对外保留旧 API(create_conversation / list_conversations / get_conversation /
add_message 等),供 ``api/conversations.py`` 和 ``api/config.py`` 调用,
但内部实现已全部改用 events 表。这是典型的"门面模式(Facade)"——接口不变,
实现替换。

【Java 类比】
  - ConversationStore ≈ Spring 的 ``@Repository`` 单例 Bean
  - ``_get_conn()``    ≈ ``DataSource.getConnection()``,每次借一个新连接
  - ``with ... as conn`` ≈ try-with-resources,JVM 自动关连接
  - dict_row(psycopg)≈ MyBatis 的 Map 映射,可以按列名取值
  - WAL 模式          ≈ MySQL 的 InnoDB,支持读写并发不阻塞
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.db import get_pg_engine

# 模块级 logger,等价于 Java 里 LoggerFactory.getLogger(getClass())
logger = logging.getLogger(__name__)

# 存储后端由 services.db 提供(PG-only,DATABASE_URL 必填)。

# DATABASE_PATH 未配置时的默认库文件路径(相对当前工作目录)。
# 【单一事实来源】main.py / engine/graph.py 等处的 env 默认值统一引用本常量,
# 避免 "data/conversations.db" 字面量散落多处——改默认库路径时只动这里。
def _now() -> str:
    """获取当前 UTC 时间的 ISO 8601 字符串(timezone-aware,带时区)。

    Java 类比: ``OffsetDateTime.now(ZoneOffset.UTC).toString()``。
    全程用 UTC 而非本地时区,避免跨时区部署时时间错乱(生产环境最佳实践)。
    """
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    """PG 持久化的会话存储(append-only 事件流,连接池见 services.db)。

    【职责】
      - 管理三张表(events / session_meta / call_logs)的 DDL 与迁移
      - 提供会话 CRUD、消息追加、事件流读写、调用日志等原子操作

    【设计模式】
      - Repository 模式:对外暴露领域方法,内部封装 SQL
      - Facade 模式:对外保留旧 API,内部走新表

    【Java 类比】
      相当于 Spring 的 ``@Repository`` Bean。与 JPA EntityManager 不同,
      这里没有 ORM,SQL 全部手写(类似 MyBatis 的原生 SQL 模式)。
      每个 public 方法都会自己开一个连接(``_get_conn``),用完即关,
      不做跨方法的事务传播——简单但足够,PG 侧由共享连接池承担并发。

    【线程安全】
      每次方法调用从连接池借还连接(池线程安全),事务语义
      "成功提交 / 异常回滚"由 services.db 的连接上下文保证。
    """

    def __init__(self, db_path: str = "", database_url: Optional[str] = None):
        """初始化存储。

        Args:
            db_path: 已废弃(PG-only 后无 SQLite 路径)。保留形参只为兼容
                旧调用签名(历史测试/外部代码传 tmp 路径),实参被忽略。
            database_url: PG 连接串;缺省走 env DATABASE_URL(必填,
                见 services.db.require_database_url)。
        """
        if db_path:
            logger.warning("db_path 已废弃(PG-only),实参被忽略——存储由 DATABASE_URL 决定")
        self.db = get_pg_engine(database_url)
        # 首次启动时建表
        self._init_db()
        logger.info("ConversationStore initialized: postgres")

    def _get_conn(self):
        """从共享连接池借一个连接(成功提交 / 异常回滚,用完归还)。"""
        return self.db.connect()

    # 三张核心表 + session_pack_state 的 DDL(两方言通用:
    # TEXT/INTEGER/IF NOT EXISTS/复合主键语义完全一致)
    _DDL = [
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,         -- 事件 ID(UUID),主键
            conv_id TEXT NOT NULL,       -- 所属会话 ID(外键语义,无约束)
            kind TEXT NOT NULL,          -- 事件类型(见模块文档)
            payload TEXT NOT NULL,       -- JSON 字符串,具体内容
            created_at TEXT NOT NULL     -- ISO 时间戳(UTC)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conv_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(conv_id, kind)",
        """CREATE TABLE IF NOT EXISTS session_meta (
            conv_id TEXT PRIMARY KEY,    -- 会话 ID,与 events.conv_id 对应
            user_id TEXT NOT NULL,       -- 归属用户
            context_key TEXT DEFAULT '', -- 宿主实体标识(嵌入模式绑定,如 entryId)
            title TEXT DEFAULT '',       -- 会话标题(列表展示)
            summary TEXT DEFAULT '',     -- 会话摘要(压缩后)
            current_config TEXT,         -- 当前配置 JSON(制品配置快照)
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL     -- 列表按此字段倒序排序
        )""",
        "CREATE INDEX IF NOT EXISTS idx_meta_user ON session_meta(user_id, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_meta_context ON session_meta(user_id, context_key, updated_at DESC)",
        """CREATE TABLE IF NOT EXISTS call_logs (
            id TEXT PRIMARY KEY,
            conv_id TEXT,                -- 可空:有些调用不属于特定会话
            call_type TEXT NOT NULL,     -- 'llm'/'upstream'/'graph'/'vector'(开放枚举,插件可扩展)
            endpoint TEXT NOT NULL,      -- 调用的接口地址
            request_data TEXT,           -- 请求体 JSON
            response_data TEXT,          -- 响应体 JSON
            status_code INTEGER,         -- HTTP 状态码
            duration_ms INTEGER,         -- 耗时(毫秒)
            error_message TEXT,          -- 异常信息
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_call_logs_conv ON call_logs(conv_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_call_logs_type ON call_logs(call_type, created_at)",
        """CREATE TABLE IF NOT EXISTS session_pack_state (
            conv_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (conv_id, scope)
        )""",
    ]

    def _init_db(self):
        """建表/建索引(幂等,``IF NOT EXISTS``)。"""
        with self._get_conn() as conn:
            for stmt in self._DDL:
                conn.execute(stmt)

    # ── Conversations(session_meta 表) ─────────────────────────

    def create_conversation(
        self,
        user_id: str,
        title: str = "",
        context_key: str = "",
    ) -> Dict[str, Any]:
        """创建新会话。

        Args:
            user_id: 用户 ID
            title:   会话标题,默认空字符串
            context_key: 宿主实体标识(嵌入模式绑定,如 entryId),默认空

        Returns:
            包含 id / userId / title / createdAt 的字典,供前端直接使用。

        副作用:同时在 events 表写一条 ``kind=checkpoint`` 事件记录"创建"动作,
        作为事件流的第一条记录,便于审计。
        """
        conv_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO session_meta (conv_id, user_id, context_key, title, summary, created_at, updated_at) VALUES (?, ?, ?, ?, '', ?, ?)",
                (conv_id, user_id, context_key, title, now, now),
            )
            # 同时写一条 events(kind=checkpoint)记录会话创建
            self._append_event(conn, conv_id, "checkpoint", {"action": "created"})
        return {"id": conv_id, "userId": user_id, "title": title, "createdAt": now}

    def find_latest_by_context(self, user_id: str, context_key: str) -> Optional[Dict[str, Any]]:
        """按 (user_id, context_key) 查该绑定下最新会话(嵌入模式会话恢复)。

        Args:
            user_id: 用户 ID
            context_key: 宿主实体标识(如 entryId)

        Returns:
            最新会话字典(与 get_conversation 同构,含 messages),没有则 None。
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT conv_id FROM session_meta WHERE user_id = ? AND context_key = ? ORDER BY updated_at DESC LIMIT 1",
                (user_id, context_key),
            ).fetchone()
        if not row:
            return None
        return self._get_conversation(row["conv_id"], user_id)

    def set_context_key(self, conv_id: str, context_key: str) -> None:
        """为会话绑定/重绑 context_key(创建场景拿到宿主实体标识后回填绑定用)。

        Args:
            conv_id: 会话 ID
            context_key: 新的宿主实体标识
        """
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE session_meta SET context_key = ?, updated_at = ? WHERE conv_id = ?",
                (context_key, now, conv_id),
            )

    def list_conversations(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """列出某用户的会话(按更新时间倒序)。只查 session_meta 表。

        为什么不 JOIN events? 因为 session_meta 是物化的读模型(CQRS),
        列表查询只需扫小表,避免对大表 events 做 GROUP BY / 聚合。
        displayTitle/message_count 用相关子查询取(嵌入模式历史对话列表
        与管理端同一套推导:真实 title > 首条用户消息截断 > 新对话)。

        Args:
            user_id: 用户 ID
            limit:   最多返回条数

        Returns:
            字典列表,字段名驼峰化(适配前端 JS 约定)。
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT m.*,
                       (SELECT COUNT(*) FROM events e
                         WHERE e.conv_id = m.conv_id
                           AND e.kind IN ('user', 'assistant', 'tool_result')) AS message_count,
                       (SELECT payload FROM events e
                         WHERE e.conv_id = m.conv_id AND e.kind = 'user'
                         ORDER BY e.created_at ASC LIMIT 1) AS first_user_payload
                  FROM session_meta m
                 WHERE m.user_id = ?
                 ORDER BY m.updated_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            # current_config 是 JSON 字符串,需反序列化为 dict
            current_config = json.loads(item["current_config"]) if item.get("current_config") else None
            first_user = ""
            if item.get("first_user_payload"):
                try:
                    first_user = str(json.loads(item["first_user_payload"]).get("content") or "")
                except (ValueError, TypeError):
                    first_user = ""
            result.append({
                "id": item["conv_id"],
                "title": item["title"] or "新对话",
                "displayTitle": self._derive_display_title(item["title"], first_user),
                "messageCount": item["message_count"],
                "currentConfig": current_config,
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            })
        return result

    def list_all_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
        q: Optional[str] = None,
        packs: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """列出全部会话(管理后台用,不限用户,支持分页与过滤)。

        Args:
            limit:  最多返回条数
            offset: 偏移量(分页)
            user_id: 非空时只看该用户的会话(按用户名精确过滤)
            q:      非空时按标题/首条消息模糊匹配(LIKE)

        Returns:
            字典列表,每条包含 userId / contextKey / messageCount / displayTitle。
            displayTitle:展示级标题——引擎只在首次产出制品时写 title,
            闲聊/追问/失败的会话永远是"新对话"(对管理端毫无辨识度),
            故按「真实 title > 首条用户消息截断 > 新对话」推导,不动存量数据。
        """
        where, params = self._admin_conversation_where(user_id, q, packs)
        sql = f"""
            SELECT m.*,
                   (SELECT COUNT(*) FROM events e
                     WHERE e.conv_id = m.conv_id
                       AND e.kind IN ('user', 'assistant', 'tool_result')) AS message_count,
                   (SELECT payload FROM events e
                     WHERE e.conv_id = m.conv_id AND e.kind = 'user'
                     ORDER BY e.created_at ASC LIMIT 1) AS first_user_payload,
                   -- 会话归属插件:最近一次路由 trace 的 pack(与 packs 过滤
                   -- 共用 _PACK_EXPR 单一事实源;M2:限定 intent_route 防其他
                   -- trace 携带 pack 键时污染归属)
                   {self._PACK_EXPR} AS last_pack
              FROM session_meta m {where}
             ORDER BY m.updated_at DESC
             LIMIT ? OFFSET ?
        """
        with self._get_conn() as conn:
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            current_config = json.loads(item["current_config"]) if item.get("current_config") else None
            first_user = ""
            if item.get("first_user_payload"):
                try:
                    first_user = str(json.loads(item["first_user_payload"]).get("content") or "")
                except (ValueError, TypeError):
                    first_user = ""
            result.append({
                "id": item["conv_id"],
                "userId": item["user_id"],
                "pack": item.get("last_pack") or "",
                "contextKey": item["context_key"] or "",
                "title": item["title"] or "新对话",
                "displayTitle": self._derive_display_title(item["title"], first_user),
                "messageCount": item["message_count"],
                "currentConfig": current_config,
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            })
        return result

    @staticmethod
    def _derive_display_title(title: Optional[str], first_user: str, max_len: int = 40) -> str:
        """展示标题推导:真实 title > 首条用户消息截断 > '新对话'。

        max_len=40:管理端列表标题列是主信息列,给足辨识度;超长悬停 tooltip 看全文。
        """
        if title and title != "新对话":
            return title
        if first_user:
            text = first_user.strip().replace("\n", " ")
            return text[:max_len] + ("…" if len(text) > max_len else "")
        return "新对话"

    def count_all_conversations(
        self,
        user_id: Optional[str] = None,
        q: Optional[str] = None,
        packs: Optional[List[str]] = None,
    ) -> int:
        """统计满足管理端过滤条件的会话总数(分页用,过滤条件与 list_all 一致)。"""
        where, params = self._admin_conversation_where(user_id, q, packs)
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM session_meta m {where}", params
            ).fetchone()
        return int(row["c"])

    # 会话的 pack 判定子查询:最近一次 intent_route trace 的 pack(无则 NULL)。
    # 限定 stage=intent_route:ctx.trace 允许任意 detail 结构,不限定的话
    # 任何带 pack 键的业务打点都会污染"会话归属插件"判定。
    # substr 守卫(不用 LIKE:模式里的 % 会撞 psycopg 占位符扫描):payload 必以 '{'
    # 开头;万一存在被手工写坏的行,降级为 NULL(该会话归"其他")而不是
    # 让整个管理端列表 500(jsonb 强转对非法 JSON 会抛错)。
    _PACK_EXPR = ("(SELECT CASE WHEN substr(e.payload, 1, 1) = '{' THEN "
                  "(e.payload::jsonb -> 'detail' ->> 'pack') ELSE NULL END "
                  "FROM events e "
                  "WHERE e.conv_id = m.conv_id AND e.kind = 'trace' "
                  "AND (CASE WHEN substr(e.payload, 1, 1) = '{' THEN "
                  "(e.payload::jsonb ->> 'stage') ELSE '' END) = 'intent_route' "
                  "ORDER BY e.created_at DESC LIMIT 1)")

    def _admin_conversation_where(self, user_id: Optional[str], q: Optional[str],
                                   packs: Optional[List[str]] = None) -> tuple:
        """拼装管理端会话列表的 WHERE 子句与参数(动态 SQL,参数化防注入)。

        packs: 插件多选过滤。元素为 pack 名;空串元素表示"其他"
        (没有任何路由 trace 的会话,如纯闲聊/失败请求)。
        """
        clauses, params = [], []
        if user_id:
            clauses.append("m.user_id = ?")
            params.append(user_id)
        if q:
            # ILIKE:模糊搜索大小写不敏感(与历史 SQLite LIKE 行为一致)
            clauses.append("(m.title ILIKE ? OR m.conv_id IN ("
                           "SELECT e.conv_id FROM events e WHERE e.kind = 'user' AND e.payload ILIKE ?))")
            params.extend([f"%{q}%", f"%{q}%"])
        if packs:
            expr = self._PACK_EXPR
            named = [p for p in packs if p]
            wants_other = any(not p for p in packs)
            parts = []
            if named:
                marks = ",".join("?" * len(named))
                parts.append(f"COALESCE({expr}, '') IN ({marks})")
                params.extend(named)
            if wants_other:
                # "其他" = 没路由记录(last_pack 为 NULL → COALESCE 空)
                parts.append(f"COALESCE({expr}, '') = ''")
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def get_conversation(self, conv_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """获取会话(含全部消息)。校验用户归属,非本人返回 None。

        Args:
            conv_id: 会话 ID
            user_id: 当前用户 ID,用于权限校验

        Returns:
            会话字典(含 messages),或 None(不存在或不属于该用户)。
        """
        return self._get_conversation(conv_id, user_id)

    def conversation_exists(self, conv_id: str, user_id: str) -> bool:
        """会话存在且属于该用户(轻量 meta 查询,不重建消息)。

        供 chat 历史加载前的归属校验:cm.load(conv_id) 本身不带权限
        过滤,不校验的话任何拿到 conv_id 的人都能借 chat 套取他人历史。
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM session_meta WHERE conv_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
        return row is not None

    def get_conversation_any_user(self, conv_id: str) -> Optional[Dict[str, Any]]:
        """获取会话(管理员视角,不校验用户)。

        Args:
            conv_id: 会话 ID

        Returns:
            会话字典或 None。
        """
        return self._get_conversation(conv_id, None)

    def _get_conversation(self, conv_id: str, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """内部:获取会话,可选地校验用户归属。

        【重建消息的逻辑】
          session_meta 不存消息内容,消息在 events 表。这里查出
          ``kind IN ('user', 'assistant', 'tool_result')`` 的事件,
          按 created_at 升序还原出对话时间线——这就是事件溯源的"重放(replay)"。

        Args:
            conv_id: 会话 ID
            user_id: 非空时校验归属,为 None 时跳过校验(管理员)

        Returns:
            会话字典(含 messages 数组),不存在返回 None。
        """
        with self._get_conn() as conn:
            if user_id:
                # 普通用户:WHERE 带 user_id 做权限过滤
                meta = conn.execute(
                    "SELECT * FROM session_meta WHERE conv_id = ? AND user_id = ?",
                    (conv_id, user_id),
                ).fetchone()
            else:
                # 管理员:不校验归属
                meta = conn.execute(
                    "SELECT * FROM session_meta WHERE conv_id = ?",
                    (conv_id,),
                ).fetchone()
            if not meta:
                return None

            # 从 events 表重建 messages(user/assistant/tool_result)
            event_rows = conn.execute(
                """SELECT * FROM events WHERE conv_id = ? AND kind IN ('user', 'assistant', 'tool_result')
                   ORDER BY created_at ASC""",
                (conv_id,),
            ).fetchall()

        meta = dict(meta)
        current_config = json.loads(meta["current_config"]) if meta.get("current_config") else None
        messages = [self._row_to_message(dict(r)) for r in event_rows]

        first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        return {
            "id": meta["conv_id"],
            "userId": meta["user_id"],
            "contextKey": meta.get("context_key") or "",
            "summary": meta.get("summary") or "",
            "title": meta["title"] or "新对话",
            "displayTitle": self._derive_display_title(meta["title"], str(first_user or "")),
            "currentConfig": current_config,
            "messages": messages,
            "createdAt": meta["created_at"],
            "updatedAt": meta["updated_at"],
        }

    def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        """删除会话。校验用户归属,删除成功返回 True。

        【级联删除】
          PG 表未建外键约束,这里手动级联:先删 session_meta,
          再删 events 里所有该会话的事件。

        Args:
            conv_id: 会话 ID
            user_id: 用户 ID(权限校验)

        Returns:
            True 表示删除了至少一行,False 表示会话不存在或不属于该用户。
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM session_meta WHERE conv_id = ? AND user_id = ?",
                (conv_id, user_id),
            )
            # 级联删除以权限校验通过为前提:meta 删 0 行(会话不存在/非本人)
            # 时绝不动 events/记忆——否则未授权请求能把别人的消息和
            # 插件记忆毁掉,meta 残留成空壳僵尸会话
            if cursor.rowcount > 0:
                conn.execute("DELETE FROM events WHERE conv_id = ?", (conv_id,))
                conn.execute(
                    "DELETE FROM session_pack_state WHERE conv_id = ?", (conv_id,))
            return cursor.rowcount > 0

    def delete_conversation_any_user(self, conv_id: str) -> bool:
        """删除会话(管理员视角,不校验归属)。级联删除 events,语义同 delete_conversation。

        Args:
            conv_id: 会话 ID。

        Returns:
            True 表示删除成功,False 表示会话不存在。
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM session_meta WHERE conv_id = ?",
                (conv_id,),
            )
            conn.execute("DELETE FROM events WHERE conv_id = ?", (conv_id,))
            conn.execute("DELETE FROM session_pack_state WHERE conv_id = ?", (conv_id,))
            return cursor.rowcount > 0

    # ── 会话级插件状态(session_pack_state 表) ──────────────────

    # 状态体积上限:插件状态是轻量 KV(选过的库/数据源/偏好),不是数据存储;
    # 超限说明插件把它当数据库用了,直接拒绝(调用方 fail-open 降级)
    PACK_STATE_MAX_BYTES = 65536

    def get_pack_state(self, conv_id: str, scope: str) -> Dict[str, Any]:
        """读会话内某 scope 的插件状态;没有返回空 dict。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT state_json FROM session_pack_state WHERE conv_id = ? AND scope = ?",
                (conv_id, scope),
            ).fetchone()
        if not row or not row["state_json"]:
            return {}
        try:
            data = json.loads(row["state_json"])
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def set_pack_state(self, conv_id: str, scope: str, state: Dict[str, Any]) -> None:
        """整包写入某 scope 的插件状态(UPSERT:ON CONFLICT 覆盖)。

        Raises:
            ValueError: 序列化后超体积上限(防插件把状态当数据存储滥用)。
        """
        payload = json.dumps(state, ensure_ascii=False)
        if len(payload.encode("utf-8")) > self.PACK_STATE_MAX_BYTES:
            raise ValueError(
                f"pack state 超过 {self.PACK_STATE_MAX_BYTES} 字节上限(scope={scope})")
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO session_pack_state
                   (conv_id, scope, state_json, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (conv_id, scope) DO UPDATE SET
                     state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at""",
                (conv_id, scope, payload, _now()))

    def update_conversation_config(
        self,
        conv_id: str,
        config: Dict[str, Any],
        title: Optional[str] = None,
    ):
        """更新会话的当前配置(current_config),可选更新标题。

        Args:
            conv_id: 会话 ID
            config:  新配置字典,会被序列化为 JSON 存储
            title:   非空时同时更新标题
        """
        now = _now()
        # ensure_ascii=False:保留中文,避免被转义成 \uXXXX(便于人读)
        config_json = json.dumps(config, ensure_ascii=False)
        with self._get_conn() as conn:
            if title:
                conn.execute(
                    "UPDATE session_meta SET current_config = ?, title = ?, updated_at = ? WHERE conv_id = ?",
                    (config_json, title, now, conv_id),
                )
            else:
                conn.execute(
                    "UPDATE session_meta SET current_config = ?, updated_at = ? WHERE conv_id = ?",
                    (config_json, now, conv_id),
                )

    def touch_conversation(self, conv_id: str):
        """只更新 updated_at 时间戳(不改变其他字段)。

        用于消息追加后让会话"浮到列表顶部"。类比 Unix 的 ``touch`` 命令。
        """
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (now, conv_id),
            )

    # ── Messages(events 表,append-only) ───────────────────────

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        config_snapshot: Optional[Dict[str, Any]] = None,
        data_artifact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """向会话追加一条消息。只 INSERT,不 UPDATE(严格遵守 append-only)。

        【实现】
          把消息包装成一条 event 写入 events 表,
          kind 根据 role 决定(user → 'user',其他 → 'assistant')。
          追加完顺手更新 session_meta.updated_at,让会话浮顶。

        Args:
            conv_id:        会话 ID
            role:           角色('user' / 'assistant' / 'tool' 等)
            content:        消息正文
            config_snapshot:当时的配置快照(可选,用于审计"用户当时配的什么")
            data_artifact:  数据型制品快照(可选,KG 检索子图等——历史恢复时
                            前端据此重渲染数据卡/图谱卡)

        Returns:
            包含 id / role / content / configSnapshot / dataArtifact / createdAt 的字典。
        """
        msg_id = str(uuid.uuid4())
        now = _now()

        # kind 映射:role -> event kind
        # 只有 'user' 单独成类,其他角色(assistant/tool/system)统一归为 assistant
        kind = "user" if role == "user" else "assistant"
        payload = {
            "role": role,
            "content": content,
            "config_snapshot": config_snapshot,
            "data_artifact": data_artifact,
        }

        with self._get_conn() as conn:
            self._append_event(conn, conv_id, kind, payload, msg_id, now)
            # 更新 session_meta 的 updated_at(让会话列表排序正确)
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (now, conv_id),
            )

        return {
            "id": msg_id,
            "role": role,
            "content": content,
            "configSnapshot": config_snapshot,
            "dataArtifact": data_artifact,
            "createdAt": now,
        }

    @staticmethod
    def _row_to_message(row: Dict[str, Any]) -> Dict[str, Any]:
        """event 行 → 前端消息字典(唯一映射,两条加载路径共用)。

        【事故记忆】这段映射曾存在两份(_get_conversation 与 get_messages
        各自内联),add_message 新增 data_artifact 字段时只改了一份——
        历史会话恢复(走 _get_conversation)拿不到制品,图谱卡消失。
        收敛到单一函数后,加字段只改这里。
        """
        payload = json.loads(row["payload"])
        return {
            "id": row["id"],
            "role": payload.get("role", row["kind"]),
            "content": payload.get("content", ""),
            "configSnapshot": payload.get("config_snapshot"),
            "dataArtifact": payload.get("data_artifact"),
            "createdAt": row["created_at"],
        }

    def get_messages(self, conv_id: str) -> List[Dict[str, Any]]:
        """获取会话全部消息(只取 user / assistant)。从 events 表重建。

        Args:
            conv_id: 会话 ID

        Returns:
            消息字典列表,按时间升序。
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM events WHERE conv_id = ? AND kind IN ('user', 'assistant')
                   ORDER BY created_at ASC""",
                (conv_id,),
            ).fetchall()

        return [self._row_to_message(dict(r)) for r in rows]

    # ── Call Logs (LLM/Upstream 调用日志) ─────────────────────

    def save_call_log(
        self,
        call_type: str,  # 'llm' or 'upstream'
        endpoint: str,
        request_data: Optional[Dict] = None,
        response_data: Optional[Dict] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        conv_id: Optional[str] = None,
    ) -> str:
        """保存一次 LLM 或上游服务调用日志(用于排查 / 审计)。

        Args:
            call_type:     'llm'(模型调用)或 'upstream'(上游业务接口)
            endpoint:      调用的接口地址 / 模型名
            request_data:  请求体(自动 JSON 序列化)
            response_data: 响应体(自动 JSON 序列化)
            status_code:   HTTP 状态码
            duration_ms:   耗时(毫秒)
            error_message: 失败时的异常信息
            conv_id:       关联会话(可空,通用调用无会话上下文)

        Returns:
            新建日志记录的 ID。
        """
        log_id = str(uuid.uuid4())
        now = _now()

        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO call_logs
                   (id, conv_id, call_type, endpoint, request_data, response_data,
                    status_code, duration_ms, error_message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_id,
                    conv_id,
                    call_type,
                    endpoint,
                    json.dumps(request_data, ensure_ascii=False) if request_data else None,
                    json.dumps(response_data, ensure_ascii=False) if response_data else None,
                    status_code,
                    duration_ms,
                    error_message,
                    now,
                ),
            )

        return log_id

    def get_call_logs(
        self,
        conv_id: Optional[str] = None,
        call_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """查询调用日志,支持按会话 / 类型过滤(四种组合)。

        Args:
            conv_id:   非空时只查该会话的日志
            call_type: 非空时只查该类型('llm' / 'upstream')
            limit:     最多返回条数

        Returns:
            日志字典列表,request_data / response_data 已自动反序列化为 dict。
        """
        # 过滤条件拼装收敛在 _call_logs_where(等价 MyBatis 的 <if> 动态 SQL),
        # 与 query_call_logs 共用一套 WHERE,避免两处分支漂移
        where, params = self._call_logs_where(conv_id, call_type)
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM call_logs {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            # 把 JSON 字符串反序列化回 dict,便于上层直接用
            if item.get("request_data"):
                item["request_data"] = json.loads(item["request_data"])
            if item.get("response_data"):
                item["response_data"] = json.loads(item["response_data"])
            result.append(item)
        return result

    def query_call_logs(
        self,
        conv_id: Optional[str] = None,
        call_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """管理端分页查询调用日志(过滤条件与 get_call_logs 一致,多分页与总数)。

        Returns:
            {"items": [...], "total": 满足条件的总条数}。items 反序列化规则同 get_call_logs。
        """
        where, params = self._call_logs_where(conv_id, call_type)
        with self._get_conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM call_logs {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM call_logs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()

        items = []
        for r in rows:
            item = dict(r)
            if item.get("request_data"):
                item["request_data"] = json.loads(item["request_data"])
            if item.get("response_data"):
                item["response_data"] = json.loads(item["response_data"])
            items.append(item)
        return {"items": items, "total": int(total)}

    def get_admin_stats(self) -> Dict[str, Any]:
        """管理端概览统计:会话/用户/消息/事件/链路打点/调用聚合/时间范围。

        单次连接内跑多条聚合 SQL(都是 COUNT/AVG/SUM,走索引,毫秒级),
        避免管理端仪表盘发一堆请求。
        """
        with self._get_conn() as conn:
            conv_count = conn.execute("SELECT COUNT(*) AS c FROM session_meta").fetchone()["c"]
            user_count = conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM session_meta"
            ).fetchone()["c"]
            event_count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            message_count = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE kind IN ('user', 'assistant')"
            ).fetchone()["c"]
            trace_count = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE kind = 'trace'"
            ).fetchone()["c"]
            span = conn.execute(
                "SELECT MIN(created_at) AS lo, MAX(created_at) AS hi FROM events"
            ).fetchone()
            call_total = conn.execute("SELECT COUNT(*) AS c FROM call_logs").fetchone()["c"]
            call_agg = conn.execute(
                """SELECT call_type, COUNT(*) AS c,
                          COALESCE(SUM(duration_ms), 0) AS ms
                     FROM call_logs GROUP BY call_type"""
            ).fetchall()
            avg_duration = conn.execute(
                "SELECT AVG(duration_ms) AS d FROM call_logs WHERE duration_ms IS NOT NULL"
            ).fetchone()["d"]

        # 按 call_type 全量透出(llm/upstream/graph/vector/未来插件自定义类型):
        # 前端照类型元数据渲染,新增调用类型不需要改这处 SQL
        calls_by_type = {
            r["call_type"]: {"count": int(r["c"]), "totalMs": int(r["ms"])}
            for r in call_agg
        }

        return {
            "conversations": int(conv_count),
            "users": int(user_count),
            "events": int(event_count),
            "messages": int(message_count),
            "traceEvents": int(trace_count),
            "firstAt": span["lo"] if span else None,
            "lastAt": span["hi"] if span else None,
            "calls": {
                "total": int(call_total),
                # 兼容旧字段(概览卡片用)
                "llm": calls_by_type.get("llm", {}).get("count", 0),
                "llmMs": calls_by_type.get("llm", {}).get("totalMs", 0),
                "upstream": calls_by_type.get("upstream", {}).get("count", 0),
                "upstreamMs": calls_by_type.get("upstream", {}).get("totalMs", 0),
                "avgDurationMs": round(float(avg_duration)) if avg_duration is not None else None,
                # 全量分类型计数(graph/vector 等新类型自动进概览)
                "byType": calls_by_type,
            },
        }

    @staticmethod
    def _call_logs_where(conv_id: Optional[str], call_type: Optional[str]) -> tuple:
        """拼装 call_logs 查询的 WHERE 子句与参数(参数化防注入)。"""
        clauses, params = [], []
        if conv_id:
            clauses.append("conv_id = ?")
            params.append(conv_id)
        if call_type:
            clauses.append("call_type = ?")
            params.append(call_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    # ── Append-only 事件流 API(新) ─────────────────────────────

    def _append_event(
        self,
        conn: Any,
        conv_id: str,
        kind: str,
        payload: Dict[str, Any],
        event_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> str:
        """内部方法:在指定连接上追加一条事件(只 INSERT)。

        【为什么不自己开连接?】
          因为它常被其他方法在同一事务内调用(例如 create_conversation
          先 INSERT session_meta 再 append_event),必须复用同一 conn
          才能保证原子性。

        Args:
            conn:      外部传入的连接(复用事务)
            conv_id:   会话 ID
            kind:      事件类型
            payload:   事件内容(会被 JSON 序列化)
            event_id:  自定义 ID,不传则自动生成 UUID
            created_at:自定义时间,不传则用当前 UTC

        Returns:
            事件 ID。
        """
        event_id = event_id or str(uuid.uuid4())
        created_at = created_at or _now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        conn.execute(
            "INSERT INTO events (id, conv_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, conv_id, kind, payload_json, created_at),
        )
        return event_id

    def append_event(
        self,
        conv_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> str:
        """公开方法:追加一条事件(append-only),并刷新会话的 updated_at。

        Args:
            conv_id: 会话 ID
            kind:    事件类型,
                     取值 ∈ {user, assistant, tool_result, compacted,
                             compact_trace, checkpoint, ask}
            payload: 事件内容字典

        Returns:
            新事件的 ID。
        """
        with self._get_conn() as conn:
            event_id = self._append_event(conn, conv_id, kind, payload)
            # 追加事件后刷新时间戳,让会话在列表里浮顶
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (_now(), conv_id),
            )
        return event_id

    def load_events(
        self,
        conv_id: str,
        kinds: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """加载会话的事件流(可按 kind 过滤,按时间升序)。

        Args:
            conv_id: 会话 ID
            kinds:   非空时只取这些 kind 的事件,None 表示全部

        Returns:
            事件字典列表,每条含 id / conv_id / kind / payload / created_at。
        """
        with self._get_conn() as conn:
            if kinds:
                # 动态构造 IN 占位符:?,?,?,... 数量等于 kinds 长度
                # 这是防 SQL 注入的标准写法(类比 Java 的 PreparedStatement)
                placeholders = ",".join("?" * len(kinds))
                rows = conn.execute(
                    f"SELECT * FROM events WHERE conv_id = ? AND kind IN ({placeholders}) ORDER BY created_at ASC",
                    (conv_id, *kinds),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE conv_id = ? ORDER BY created_at ASC",
                    (conv_id,),
                ).fetchall()

        result = []
        for r in rows:
            r = dict(r)
            result.append({
                "id": r["id"],
                "conv_id": r["conv_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            })
        return result


