"""数据源管理 —— 注册表 CRUD + Fernet 凭据加密 + 双方言执行器 + 健康检查。

移植来源: chat-bi
  - backend/app/db/models.py DataSource/SemanticModel 表结构 → chatbi schema DDL(models.py CHATBI_DDL)
  - backend/app/core/security.py Fernet 数据源密码加密(#38/#44 教训保留)
  - backend/app/services/sql_executor.py 执行器(READ ONLY 事务/DB 侧超时/行数上限三层防护)
  - backend/app/services/datasource_health.py 健康检查
适配: SQLAlchemy async → psycopg3/pymysql 同步;JWT/租户删除;registry 走 PackRelationalDB。

安全三层防护(移植保留):
  1. SQL 层校验(security/sql_validator.py, 调用方先过)
  2. READ ONLY 事务 — 即使校验被绕过也不能写
  3. DB 侧超时: PG statement_timeout / MySQL max_execution_time + 行数上限截断
"""
from __future__ import annotations

import logging
import json
import os
import time
import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from domains.chatbi.models import CHATBI_DDL, DataSourceInfo
from sdk.tool import PackInvokeError

logger = logging.getLogger(__name__)

# ── Fernet 凭据加密(#38: 密钥与密文同机有风险,生产建议 KMS;至少不硬编码 #44)──

_fernet: Fernet | None = None
_fernet_key: str = ""


def configure_encryption(fernet_key: str) -> None:
    """注入 Fernet 密钥(宿主 env FERNET_KEY / 测试显式注入)。

    未配置时首次 encrypt/decrypt 抛错——数据源密码必须加密存储,不做明文降级。
    """
    global _fernet, _fernet_key
    if fernet_key and fernet_key != _fernet_key:
        _fernet = Fernet(fernet_key.encode())
        _fernet_key = fernet_key


def _ensure_fernet() -> None:
    """懒初始化:显式注入优先,否则读 env FERNET_KEY(settings.schema 声明的兜底源)。

    仍未配置 → 保持 None,由调用方 fail-closed 报错。
    """
    if _fernet is None:
        configure_encryption(os.getenv("FERNET_KEY", ""))


def encrypt_password(plain: str) -> str:
    _ensure_fernet()
    if _fernet is None:
        raise RuntimeError("FERNET_KEY 未配置——数据源密码必须加密存储(fail-closed,"
                           "在 .env 配置 FERNET_KEY=openssl rand -hex 32 生成)")
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    _ensure_fernet()
    if _fernet is None:
        raise RuntimeError("FERNET_KEY 未配置——无法解密数据源密码(fail-closed)")
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except InvalidToken as e:
        raise ValueError("数据源密码解密失败(密钥不匹配或密文损坏)") from e


# ── 注册表 CRUD(chatbi_data_sources) ─────────────────────────

# mcp: host=server code, database=tool_name(可空), port=0, 密码空串直存
_SUPPORTED_DB_TYPES = ("mysql", "postgresql", "mcp")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_store(db) -> None:
    """幂等建表(db = PackRelationalDB("chatbi"))。"""
    db.init_schema(CHATBI_DDL)


def _row_to_info(row: dict) -> DataSourceInfo:
    return DataSourceInfo(
        id=row["id"], name=row["name"], db_type=row["db_type"],
        host=row["host"], port=int(row["port"]), database=row["database"],
        username=row["username"], encrypted_password=row["encrypted_password"],
        is_active=bool(row["is_active"]),
        scan_status=row.get("scan_status") or "idle",
        scan_progress=int(row.get("scan_progress") or 0),
        scan_stage=row.get("scan_stage") or "",
        scan_error=row.get("scan_error") or "",
        scanned_at=row.get("scanned_at") or "",
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
    )


def create_datasource(db, name: str, db_type: str, host: str, port: int,
                      database: str, username: str, password: str,
                      is_active: bool = True) -> DataSourceInfo:
    """新建数据源(密码 Fernet 加密落库;密文永不回传)。

    mcp 例外: 无凭据可加密, 密码空串直存空串(跳过 Fernet);
    host 必填(=server code)。
    """
    if db_type not in _SUPPORTED_DB_TYPES:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    if db_type == "mcp" and not (host or "").strip():
        raise ValueError("mcp 数据源 host 必填(=MCP server code)")
    encrypted = ("" if (db_type == "mcp" and not password)
                 else encrypt_password(password))
    ds_id = str(uuid.uuid4())
    now = _now()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO chatbi_data_sources
               (id, name, db_type, host, port, database, username,
                encrypted_password, is_active, scan_status, scan_progress, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', 0, ?, ?)""",
            (ds_id, name, db_type, host, int(port), database, username,
             encrypted, 1 if is_active else 0, now, now))
    return get_datasource(db, ds_id, decrypt=True)


def get_datasource(db, ds_id: str, decrypt: bool = False) -> DataSourceInfo | None:
    """按 id 取数据源;decrypt=True 时解密密码到 password_plain(运行时用)。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM chatbi_data_sources WHERE id = ?", (ds_id,)).fetchone()
    if not row:
        return None
    info = _row_to_info(dict(row))
    if decrypt and info.encrypted_password:
        info.password_plain = decrypt_password(info.encrypted_password)
    return info


def list_datasources(db, active_only: bool = False) -> list:
    with db.connect() as conn:
        sql = "SELECT * FROM chatbi_data_sources"
        if active_only:
            sql += " WHERE is_active = 1"
        rows = conn.execute(f"{sql} ORDER BY created_at DESC").fetchall()
    return [_row_to_info(dict(r)) for r in rows]


def _current_db_type(db, ds_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT db_type FROM chatbi_data_sources WHERE id = ?", (ds_id,)).fetchone()
    return row["db_type"] if row else ""


def update_datasource(db, ds_id: str, **fields) -> bool:
    """更新可变字段。password 特殊处理:明文 → Fernet 加密;列名白名单防注入。

    mcp 行密码空串直存空串(与 create 同契约);db_type 值同 create 白名单。
    """
    if "db_type" in fields and fields["db_type"] not in _SUPPORTED_DB_TYPES:
        raise ValueError(f"不支持的数据库类型: {fields['db_type']}")
    allowed = {"name", "db_type", "host", "port", "database", "username",
               "is_active", "scan_status", "scan_progress", "scan_stage",
               "scan_error", "scanned_at"}
    cols, params = [], []
    for k, v in fields.items():
        if k == "password":
            cols.append("encrypted_password = ?")
            if v == "" and (fields.get("db_type") or _current_db_type(db, ds_id)) == "mcp":
                params.append("")  # mcp 无凭据: 空串直存
            else:
                params.append(encrypt_password(v))
        elif k in allowed:
            cols.append(f"{k} = ?")
            params.append(int(v) if k in ("port", "is_active", "scan_progress") else v)
        else:
            raise ValueError(f"datasource column not updatable: {k}")
    if not cols:
        return False
    cols.append("updated_at = ?")
    params.append(_now())
    params.append(ds_id)
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE chatbi_data_sources SET {', '.join(cols)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_datasource(db, ds_id: str) -> bool:
    """删除数据源(级联清理调用方负责:语义版本/向量 collection/记忆)。"""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM chatbi_data_sources WHERE id = ?", (ds_id,))
        return cur.rowcount > 0


def resolve_datasource(db, ds_id: str | None = None,
                       decrypt: bool = True) -> DataSourceInfo:
    """解析目标数据源:显式 id > 会话绑定(调用方) > 第一个 active(fail-fast)。

    运行时入口:工具管线用它拿到带明文密码的连接信息。
    """
    if ds_id:
        info = get_datasource(db, ds_id, decrypt=decrypt)
        if not info:
            raise ValueError(f"数据源不存在: {ds_id}")
        if not info.is_active:
            raise ValueError(f"数据源已停用: {info.name}")
        return info
    actives = list_datasources(db, active_only=True)
    if not actives:
        raise ValueError("尚无可用数据源——请在管理端添加并扫描数据源")
    return get_datasource(db, actives[0].id, decrypt=decrypt)


# ── 执行器(双方言;READ ONLY + DB 侧超时 + 行数上限) ────────────

class ExecuteResult:
    def __init__(self, ok: bool, columns=None, rows=None, rowcount: int = 0,
                 duration_ms: int = 0, error: str = "", truncated: bool = False):
        self.ok = ok
        self.columns = columns or []
        self.rows = rows or []
        self.rowcount = rowcount
        self.duration_ms = duration_ms
        self.error = error
        self.truncated = truncated


def _pg_connect(info: DataSourceInfo, timeout_seconds: int,
                connect_timeout: int = 5):
    import psycopg
    return psycopg.connect(
        host=info.host, port=info.port, user=info.username,
        password=info.password_plain, dbname=info.database,
        connect_timeout=connect_timeout, autocommit=False,
        options=f"-c statement_timeout={timeout_seconds * 1000}")


def _mysql_connect(info: DataSourceInfo, connect_timeout: int = 5):
    import pymysql
    return pymysql.connect(
        host=info.host, port=info.port, user=info.username,
        password=info.password_plain, database=info.database,
        connect_timeout=connect_timeout, charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor)


def _inject_limit(sql: str, max_rows: int, db_type: str) -> str:
    """无顶层 LIMIT 的 SELECT 自动加 LIMIT(max_rows+1)(防 DB 端全表扫描)。

    移植源 sql_executor._inject_limit(v1 教训 #46: sqlglot AST 判断顶层
    Limit, 不用字符串检测; 子查询的 LIMIT 不算——外层仍可能全表扫描)。
    fetchmany 只限客户端取行, DB 端排序/聚合/扫描仍按全表执行,
    LIMIT 注入是 DB 侧防护, 两者互补。parse 失败 → 原样返回
    (三层校验已拦截非法 SQL, 这里只做增强不拦截)。
    """
    fetch_rows = max_rows + 1  # 多取 1 行用于判断是否截断
    try:
        import sqlglot
        dialect = "mysql" if db_type == "mysql" else "postgres"
        stmt = sqlglot.parse_one(sql, read=dialect)
        if stmt.args.get("limit") is not None:
            return sql
        return stmt.limit(fetch_rows).sql(dialect=dialect)
    except Exception:
        return sql


def execute_readonly(info: DataSourceInfo, sql: str,
                     max_rows: int = 10000, timeout_seconds: int = 30,
                     connect_timeout: int = 5) -> ExecuteResult:
    """只读执行 BI 查询(READ ONLY 事务 + DB 侧超时 + max_rows 截断)。"""
    started = time.monotonic()
    sql = _inject_limit(sql, max_rows, info.db_type)
    try:
        if info.db_type == "postgresql":
            conn = _pg_connect(info, timeout_seconds, connect_timeout)
            try:
                cur = conn.cursor()
                # SEC: READ ONLY 事务 — 防御性约束, 即使校验被绕过也不能写
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
                conn.commit()
                duration = int((time.monotonic() - started) * 1000)
                return ExecuteResult(True, columns, [tuple(r) for r in rows],
                                     len(rows), duration, truncated=truncated)
            finally:
                conn.close()
        elif info.db_type == "mysql":
            conn = _mysql_connect(info, connect_timeout)
            try:
                with conn.cursor() as cur:
                    # DB 侧超时(MySQL 5.7+;毫秒)
                    cur.execute(f"SET SESSION max_execution_time = {timeout_seconds * 1000}")
                    # SEC: READ ONLY 事务(MySQL 5.6.5+;与 PG 路径同防御纵深,
                    # 更旧版本会报语法错误——由 except 统一捕获返回执行失败)
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(sql)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    rows = cur.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    rows = rows[:max_rows]
                conn.commit()
                duration = int((time.monotonic() - started) * 1000)
                return ExecuteResult(True, columns, [tuple(r) for r in rows],
                                     len(rows), duration, truncated=truncated)
            finally:
                conn.close()
        raise ValueError(f"不支持的数据库类型: {info.db_type}")
    except Exception as e:
        duration = int((time.monotonic() - started) * 1000)
        logger.warning("数据源执行失败(%s, %dms): %s", info.db_type, duration, str(e)[:160])
        return ExecuteResult(False, error=_clean_db_error(e), duration_ms=duration)


def _clean_db_error(e: Exception) -> str:
    """执行错误 → 用户可读文本(去连接串等敏感细节)。"""
    text = str(e)
    for token in ("password=", "user=", "dbname=", "host="):
        text = text.replace(token, "***")
    # psycopg 异常很长,首行通常是核心信息
    return text.splitlines()[0][:300] if text else "执行失败"


# ── MCP 执行器(工具调用 JSON → ExecuteResult; ask_data execute 步消费) ──

def execute_mcp_tool(info: DataSourceInfo, tool_call: dict,
                     max_rows: int = 10000) -> ExecuteResult:
    """执行 MCP 工具调用并映射为 ExecuteResult(与 execute_readonly 同契约)。

    tool_call: {"tool": str, "args": dict}(也容忍 JSON 字符串——解析失败
    ok=False)。工具白名单: info.database 非空(=tool_name 限定)时只允许
    该工具;语义层工具名校验由 ask_data generate 步负责(fail-closed)。

    结果形状映射(mcp_client.call_tool 返回值):
      dict 含 columns+rows(rows list[list]/list[tuple]) → 直接用(tuple 化)
      list[dict] → 首行 keys 为 columns、值转 tuple
      裸 dict(无 columns/rows) → 单条记录(键即列名)
      list[list] → 无列名 → ok=False(不猜列名)
      list[标量] / 标量 → 单列 value
      空 list → 空结果(ok)
    行数超 max_rows 截断(truncated=True)。PackInvokeError → error=str(e)
    如实上报(不静默——外部 server 故障对用户可见)。
    """
    started = time.monotonic()

    def _fail(msg: str) -> ExecuteResult:
        duration = int((time.monotonic() - started) * 1000)
        logger.warning("MCP 工具调用失败(%s, %dms): %s", info.host, duration, msg[:160])
        return ExecuteResult(False, error=msg, duration_ms=duration)

    # 1. 解析与形状校验(ask_data 传 JSON 字符串; 直调方可传 dict)
    if isinstance(tool_call, str):
        try:
            tool_call = json.loads(tool_call)
        except (ValueError, TypeError) as e:
            return _fail(f"工具调用 JSON 解析失败: {e}")
    if not isinstance(tool_call, dict):
        return _fail(f"工具调用必须是 JSON 对象, 实为 {type(tool_call).__name__}")
    tool = tool_call.get("tool")
    args = tool_call.get("args")
    if not isinstance(tool, str) or not tool.strip():
        return _fail(f"工具调用缺少有效 tool 名: {tool!r}")
    if not isinstance(args, dict):
        return _fail(f"工具调用 args 必须是 JSON 对象, 实为 {type(args).__name__}")
    # 2. 限定过滤(info.database=tool_name 时只允许该工具)
    if (info.database or "").strip() and tool != info.database:
        return _fail(f"工具 {tool} 不在数据源限定范围内(仅允许 {info.database})")

    # 3. 委托共享执行内核(P2/设计 4.4:传输+信封判定+inputSchema 验参
    # 一处实现——此前 {status: forbidden} 会被当成功数据行,现按业务
    # 状态如实分类;ask_data/switch_chart 的自愈回环等交互副作用在外层不变)
    from domains.chatbi.controlled import _invoke_mcp_core
    core = _invoke_mcp_core(info, tool, args, started, source=None)
    duration = int((time.monotonic() - started) * 1000)
    if not core.ok:
        msg = core.error.message if core.error else core.status
        logger.warning("MCP 工具调用失败(%s, %dms): %s",
                       info.host, duration, msg[:160])
        return ExecuteResult(False, error=msg, duration_ms=duration)
    data = core.data or {}
    if "columns" in data and "rows" in data:
        columns = [str(c) for c in data["columns"]]
        rows = [tuple(r) if isinstance(r, (list, tuple)) else (r,)
                for r in data["rows"]]
    elif "record" in data and isinstance(data["record"], dict):
        columns = [str(k) for k in data["record"].keys()]
        rows = [tuple(data["record"].get(c) for c in columns)]
    else:  # 内核契约兜底(理论不可达:_normalize_tabular 只出这两种形状)
        return ExecuteResult(False, error="上游返回格式不符合表格契约",
                             duration_ms=duration)

    # 4. 截断(行数上限;映射与信封判定已由共享内核完成)
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    duration = int((time.monotonic() - started) * 1000)
    return ExecuteResult(True, columns, rows, len(rows), duration,
                         truncated=truncated)


# ── 健康检查(datasource_health 移植) ──────────────────────────

def check_health(info: DataSourceInfo, timeout_seconds: int = 5) -> dict:
    """连通性 + 延迟探测。返回 {healthy, latency_ms, server_version|error}。"""
    started = time.monotonic()
    try:
        # 连接超时与探测超时共用(timeout_seconds 即探测预算)
        if info.db_type == "mcp":
            # mcp 探测: list_tools(server code) 成功即健康。
            # 模块引用形式(非 from ... import 名字绑定)——测试 monkeypatch
            # 打在 sdk.mcp_client 模块属性上, 名字绑定会让替换失效。
            from sdk import mcp_client
            tools = mcp_client.list_tools(info.host)
            version = f"MCP {info.host}({len(tools)} tools)"
        elif info.db_type == "postgresql":
            conn = _pg_connect(info, timeout_seconds,
                               connect_timeout=timeout_seconds)
            try:
                cur = conn.cursor()
                cur.execute("SELECT version()")
                version = cur.fetchone()[0].split(",")[0]
            finally:
                conn.close()
        elif info.db_type == "mysql":
            conn = _mysql_connect(info, connect_timeout=timeout_seconds)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT VERSION()")
                    version = f"MySQL {cur.fetchone()[0]}"
            finally:
                conn.close()
        else:
            raise ValueError(f"不支持的数据库类型: {info.db_type}")
        return {"healthy": True, "latency_ms": int((time.monotonic() - started) * 1000),
                "server_version": version}
    except Exception as e:
        error = (f"MCP server {info.host} 探测失败: {_clean_db_error(e)}"
                 if info.db_type == "mcp" else _clean_db_error(e))
        return {"healthy": False, "latency_ms": int((time.monotonic() - started) * 1000),
                "error": error}


# ── 批量健康巡检(源 datasource_health.check_all_datasources_health 移植) ──
# 连续失败计数: 进程内存(源 _health_fail_counts 同为进程态;重启归零可接受)
_health_fail_counts: dict[str, int] = {}
HEALTH_MAX_FAILURES = 3  # 连续失败 3 次自动停用(源 datasource_health_check_max_failures)


def check_all_health(db, max_failures: int = HEALTH_MAX_FAILURES) -> dict:
    """巡检全部数据源(含已停用的——给恢复机会, 源同款语义)。

    - ping 成功 + is_active=False → 恢复 is_active=True
    - ping 失败 → 连续计数 +1; 达 max_failures → is_active=False(自动隔离,
      查询侧 resolve_datasource 不再选中, 不用等用户查询报错才发现)
    - 单源失败不影响其他源;不抛(定时任务容错)
    """
    summary = {"checked": 0, "healthy": 0, "unhealthy": 0,
               "recovered": 0, "newly_deactivated": 0, "items": []}
    for row in list_datasources(db, active_only=False):
        summary["checked"] += 1
        # 解密密码再探测: list_datasources 不带 decrypt, password_plain 为空,
        # 直接 ping 必失败 → 15 分钟内所有数据源被误停用(真实事故)。
        # 单源解密失败(密钥轮换后旧密文)只标记该源 unhealthy,不拖垮
        # 整轮巡检——巡检是逐源独立的,一个坏源 500 掉全部观测不可接受
        try:
            info = get_datasource(db, row.id, decrypt=True) or row
        except Exception as e:  # noqa: BLE001
            logger.warning("数据源 %s 密码解密失败(密钥不匹配或密文损坏), "
                           "标记 unhealthy: %s", row.name, e)
            summary["unhealthy"] += 1
            _health_fail_counts[row.id] = _health_fail_counts.get(row.id, 0) + 1
            if (_health_fail_counts[row.id] >= max_failures
                    and row.is_active):
                update_datasource(db, row.id, is_active=False)
                summary["newly_deactivated"] += 1
            summary["items"].append({
                "id": row.id, "name": row.name, "healthy": False,
                "error": "密码解密失败(密钥不匹配或密文损坏),请编辑数据源重存密码"})
            continue
        result = check_health(info)
        ds_id = info.id
        if result.get("healthy"):
            summary["healthy"] += 1
            _health_fail_counts.pop(ds_id, None)
            if not info.is_active:
                update_datasource(db, ds_id, is_active=True)
                summary["recovered"] += 1
                logger.info("数据源 %s 健康恢复, 重新启用", info.name)
        else:
            summary["unhealthy"] += 1
            _health_fail_counts[ds_id] = _health_fail_counts.get(ds_id, 0) + 1
            if _health_fail_counts[ds_id] >= max_failures and info.is_active:
                update_datasource(db, ds_id, is_active=False)
                summary["newly_deactivated"] += 1
                logger.warning("数据源 %s 连续 %d 次健康检查失败, 自动停用",
                               info.name, _health_fail_counts[ds_id])
        summary["items"].append({"id": ds_id, "name": info.name,
                                 "healthy": result.get("healthy", False),
                                 "latencyMs": result.get("latency_ms"),
                                 "isActive": info.is_active})
    return summary
