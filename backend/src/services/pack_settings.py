"""PackSettings - 插件配置的存储 / schema 解析 / 三级合并链。

【模块定位】
插件的"声明式配置页"数据层。pack 在自己的 settings.schema.yaml 里声明
配置字段(类型/标签/默认值/env 兜底),管理端渲染成通用表单;保存的值进
pack_settings 表。运行时与依赖检测都从这里解析最终值。

【配置优先级】(高 → 低)
  1. 管理端设置页保存值(pack_settings 表)
  2. 环境变量(字段声明的 env 名,如 NEO4J_URI)
  3. schema 声明的 default

【与依赖检测的关系】
依赖检测(services/pack_dependency.py)按"字段是否解析出非空值"判定,
因此运维可以选择:要么部署时配 env,要么部署后在插件中心设置页补配——
两条路都能让依赖满足(补配后点"重新检测"热加载,无需重启)。

【settings.schema.yaml 格式】
    version: 1
    groups:
      - key: connection
        title: 连接配置
        fields:
          - {key: neo4j_uri, type: string, label: Neo4j 地址, env: NEO4J_URI, required: true}
          - {key: neo4j_password, type: secret, label: Neo4j 密码, env: NEO4J_PASSWORD, required: true}
          - {key: retrieval_top_k, type: int, label: 检索条数, default: 5, min: 1, max: 50}
          - {key: schema_mode, type: enum, label: 模式, options: [strict, semi_open], default: semi_open}

  type 取值:string / int / bool / enum / secret。
  secret 类型读取时永不回显(掩码哨兵),只在保存新值时覆盖。

【Java 类比】
PackSettingsStore ≈ @Repository;resolve 链 ≈ Spring Environment 的
PropertySource 优先级排序(命令行 > 配置文件 > 默认值)。
"""
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# secret 字段"已配置但不回显"的哨兵值。前端看到它就知道:有值,留空提交则保持不变。
SECRET_SET = "__SET__"

# domains/ 目录根(扫描 settings.schema.yaml 用,不 import pack 模块)
_DOMAINS_DIR = Path(__file__).resolve().parent.parent / "domains"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── schema 读取(零 import:只读文件) ─────────────────────────────

def read_settings_schema(pack_name: str) -> Optional[Dict[str, Any]]:
    """读取 pack 的 settings.schema.yaml(不 import pack 模块)。

    依赖检测必须在"决定是否 import pack"之前读配置,所以这里只做纯文件
    读取;文件不存在/损坏返回 None(= 该 pack 无声明式配置页)。
    """
    path = _DOMAINS_DIR / pack_name / "settings.schema.yaml"
    if not path.exists():
        return None
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"读取 {pack_name} settings.schema.yaml 失败: {e}")
        return None


def iter_fields(schema: Optional[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """平铺迭代 schema 里全部字段定义(跨 group)。"""
    for group in ((schema or {}).get("groups") or []):
        for field in (group.get("fields") or []):
            if isinstance(field, dict) and field.get("key"):
                yield field


def find_field(schema: Optional[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """按 key 查找字段定义。"""
    for field in iter_fields(schema):
        if field.get("key") == key:
            return field
    return None


# ── 解析链(保存值 > env > default) ───────────────────────────────

def resolve_value(field: Dict[str, Any], saved: Dict[str, Any]) -> Any:
    """单个字段过三级合并链。空字符串/None 视为"未提供",落到下一级。"""
    key = field.get("key")
    v = saved.get(key)
    if v is not None and str(v).strip() != "":
        return v
    env_name = field.get("env")
    if env_name:
        ev = os.getenv(str(env_name), "").strip()
        if ev:
            return ev
    return field.get("default")


def resolve_all(
    pack_name: str,
    saved: Optional[Dict[str, Any]] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """全部字段过合并链,返回 {key: 最终值}。

    schema 不传则现读文件;依赖检测与 pack 运行时共用这一条解析路径,
    保证"检测认为可用"与"运行时拿到的连接信息"永远一致。
    """
    if schema is None:
        schema = read_settings_schema(pack_name)
    saved = saved or {}
    return {f["key"]: resolve_value(f, saved) for f in iter_fields(schema)}


def resolve_with_saved(pack_name: str, store: Optional["PackSettingsStore"]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """读库取保存值 + 解析全字段(依赖检测入口用)。

    Returns:
        (resolved, schema)。store 为 None 时保存值视为空(纯 env 模式,
        部分测试场景 / 表未建时使用)。
    """
    schema = read_settings_schema(pack_name)
    saved = store.get_values(pack_name) if store is not None else {}
    return resolve_all(pack_name, saved=saved, schema=schema), schema


# ── 校验与掩码 ──────────────────────────────────────────────────

def validate_values(
    schema: Optional[Dict[str, Any]],
    values: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """按 schema 校验并规范化待保存的值。

    Returns:
        (clean, errors)。clean 是通过校验的部分(类型已转换);
        errors 是 [{field, message}] 列表,有错时调用方应整体拒绝。

    secret 哨兵处理:值等于 SECRET_SET 表示"前端没改,保持旧值",
    调用方(save_values 前的合并逻辑)会跳过这些键,clean 里不含它们。
    """
    clean: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for key, raw in (values or {}).items():
        field = find_field(schema, key)
        if field is None:
            # schema 外的键拒绝:防止拼写错误的键静默沉到库里
            errors.append({"field": key, "message": "未知配置项(不在 settings.schema.yaml 中)"})
            continue
        ftype = str(field.get("type") or "string")
        v: Any = raw
        # 哨兵先判(非空值,原写法放在空值分支里永不可达):secret 字段的
        # "__SET__" = "前端没改,保持旧值",调用方合并时跳过
        if ftype in ("secret", "string") and isinstance(raw, str) and raw.strip() == SECRET_SET:
            continue
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            # 空值 = 清除该项(回落到 env/默认)
            clean[key] = None
            continue
        try:
            if ftype == "int":
                v = int(raw)
                if "min" in field and v < int(field["min"]):
                    raise ValueError(f"小于最小值 {field['min']}")
                if "max" in field and v > int(field["max"]):
                    raise ValueError(f"大于最大值 {field['max']}")
            elif ftype == "bool":
                if isinstance(raw, str):
                    v = raw.strip().lower() in ("1", "true", "yes", "on")
                else:
                    v = bool(raw)
            elif ftype == "enum":
                v = str(raw)
                options = field.get("options") or []
                if options and v not in [str(o) for o in options]:
                    raise ValueError(f"必须是 {options} 之一")
            else:  # string / secret
                v = str(raw).strip()
        except (TypeError, ValueError) as e:
            errors.append({"field": key, "message": str(e)})
            continue
        clean[key] = v
    return clean, errors


def mask_secrets(schema: Optional[Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, Any]:
    """secret 字段替换为掩码哨兵(GET 响应用,永不回显明文)。

    schema 外的遗留键直接丢弃:schema 演进(改名/删除)后,旧保存值不在
    当前 schema 里,无从判断是否 secret——按最坏情况不回显。
    """
    schema_keys = {field["key"] for field in iter_fields(schema)}
    masked = {k: v for k, v in (values or {}).items() if k in schema_keys}
    for field in iter_fields(schema):
        if str(field.get("type") or "") == "secret":
            key = field["key"]
            if masked.get(key):
                masked[key] = SECRET_SET
            else:
                masked[key] = ""
    return masked


# ── 存储层 ──────────────────────────────────────────────────────

class PackSettingsStore:
    """pack_settings 表的 DAO(pack_name → values JSON)。

    【线程安全】同 ConversationStore 模式:每方法新开连接 + WAL,
    多线程读 / 单线程写,无跨方法事务。
    """

    def __init__(self, db_path: str):
        from services.conversation_store import DEFAULT_DB_PATH
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 写操作串行化(读走新连接,无需锁)
        self._init_db()
        logger.info(f"PackSettingsStore initialized: {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """幂等建表(与 ConversationStore 同库不同表,互不影响)。

        注意列名用 values_json:"values" 是 SQLite 关键字,不能直接作列名。
        """
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pack_settings (
                    pack_name  TEXT PRIMARY KEY,  -- pack 名(目录名)
                    values_json TEXT NOT NULL,    -- JSON: {字段key: 保存值}
                    updated_at TEXT NOT NULL      -- ISO 时间戳(UTC)
                );
            """)

    def get_values(self, pack_name: str) -> Dict[str, Any]:
        """读某 pack 的全部保存值(原始,未掩码;无记录返回空 dict)。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT values_json FROM pack_settings WHERE pack_name = ?", (pack_name,)
            ).fetchone()
        if not row or not row["values_json"]:
            return {}
        try:
            data = json.loads(row["values_json"])
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            logger.warning(f"pack_settings.{pack_name} 的 values_json 损坏,按空处理")
            return {}

    def save_values(self, pack_name: str, values: Dict[str, Any]) -> None:
        """合并保存:提供键覆盖/清除(None 删除),未提供键保持;整体落一行 JSON。"""
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT values_json FROM pack_settings WHERE pack_name = ?", (pack_name,)
            ).fetchone()
            current: Dict[str, Any] = {}
            if row and row["values_json"]:
                try:
                    current = json.loads(row["values_json"]) or {}
                except (ValueError, TypeError):
                    current = {}
            for k, v in (values or {}).items():
                if v is None:
                    current.pop(k, None)   # 显式清除:回落 env / 默认
                else:
                    current[k] = v
            conn.execute(
                """INSERT INTO pack_settings (pack_name, values_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(pack_name) DO UPDATE SET values_json = excluded.values_json,
                                                       updated_at = excluded.updated_at""",
                (pack_name, json.dumps(current, ensure_ascii=False), _now()),
            )

    def delete(self, pack_name: str) -> None:
        """删除某 pack 的全部保存值(pack 目录被删时清理用)。"""
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM pack_settings WHERE pack_name = ?", (pack_name,))


# ── pack 运行时读取门面 ──────────────────────────────────────────

class PackSettingsReader:
    """pack 运行时的配置读取器(每次读实时解析,管理端热改即时生效)。

    pack.py 装配时构造并注入自己的 client/工具:
        reader = PackSettingsReader("knowledge_graph", settings_store)
        uri = reader.get("neo4j_uri", "bolt://localhost:7687")
    """

    def __init__(self, pack_name: str, store: Optional[PackSettingsStore] = None):
        self._pack_name = pack_name
        self._store = store

    def get(self, key: str, default: Any = None) -> Any:
        """读单字段最终值(保存值 > env > schema default);都没有返回 default。"""
        schema = _schema_cached(self._pack_name)
        field = find_field(schema, key)
        if field is None:
            # schema 没声明的键:只剩 env 兜底(约定 env 名 = 键大写)
            return os.getenv(key.upper(), default)
        saved = self._store.get_values(self._pack_name) if self._store else {}
        v = resolve_value(field, saved)
        return default if v in (None, "") else v

    def all(self) -> Dict[str, Any]:
        """全字段最终值(给探针 / client 构造整包传递)。"""
        resolved, _ = resolve_with_saved(self._pack_name, self._store)
        return resolved


# schema 文件解析缓存:{pack_name: (mtime, size, parsed)}。读取方(如 KG
# 导入循环按批按键取配置)高频调 get,每次重读+yaml 解析是纯浪费;
# 按 (mtime, size) 失效,设置文件改动立即反映。
_schema_cache: Dict[str, tuple] = {}
_schema_cache_lock = threading.Lock()


def _schema_cached(pack_name: str) -> Optional[Dict[str, Any]]:
    path = _DOMAINS_DIR / pack_name / "settings.schema.yaml"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _schema_cache_lock:
            _schema_cache.pop(pack_name, None)
        return None if not path.exists() else read_settings_schema(pack_name)
    with _schema_cache_lock:
        hit = _schema_cache.get(pack_name)
        if hit and hit[0] == stamp:
            return hit[1]
    parsed = read_settings_schema(pack_name)
    if parsed is not None:
        with _schema_cache_lock:
            _schema_cache[pack_name] = (stamp, parsed)
    return parsed
