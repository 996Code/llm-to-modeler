"""PlatformSettings - 平台级 KV 设置的存储。

【模块定位】
平台自身(非插件)的少量全局配置数据层。与管理端"插件设置"
(services/pack_settings.py,pack 级)区分:这里存的是平台级键值,
当前消费者:「显示设置」(对话界面的链路/检索结果显示开关,
管理端配置、独立/嵌入对话页共读)与「MCP 接入设置」
(对外 MCP 端点的全局单 key)。

【存储形态】
platform_settings 表(key TEXT PK / value JSON / updated_at),
幂等建表参照 PackSettingsStore 模式:每方法新开连接 + 写锁串行化,
JSON 序列化为 TEXT 存 value_json(与 pack_settings 同口径,历史
SQLite 数据零漂移;不依赖 JSONB 方言特性)。

【Java 类比】
PlatformSettingsStore ≈ @Repository(一张通用 KV 表);
get_platform_settings_store ≈ ApplicationContext 里懒初始化的单例 Bean。
"""
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()



# ── MCP 接入设置(mcp 键;对外 MCP 端点的全局单 key) ──

MCP_KEY = "mcp"
MCP_DEFAULTS: Dict[str, str] = {"api_key": ""}


def get_mcp_settings(store: Optional["PlatformSettingsStore"]) -> Dict[str, str]:
    """读 MCP 设置:损坏/缺失回默认(key 为空串=未启用,fail-closed)。"""
    saved = store.get(MCP_KEY) if store is not None else None
    saved = saved if isinstance(saved, dict) else {}
    key = saved.get("api_key")
    return {"api_key": key if isinstance(key, str) else MCP_DEFAULTS["api_key"]}


def save_mcp_settings(
    store: "PlatformSettingsStore", payload: Dict[str, Any]
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """校验并保存 MCP 设置(只认 api_key 一个键,必须非空字符串可清空)。"""
    errors: List[Dict[str, str]] = []
    clean: Dict[str, Any] = {}
    for key, raw in (payload or {}).items():
        if key not in MCP_DEFAULTS:
            errors.append({"field": key, "message": "未知配置项(只支持 api_key)"})
            continue
        if not isinstance(raw, str):
            errors.append({"field": key, "message": "api_key 必须是字符串"})
            continue
        clean[key] = raw
    if errors:
        return get_mcp_settings(store), errors
    merged = get_mcp_settings(store)
    merged.update(clean)
    store.set(MCP_KEY, merged)
    return merged, []


# ── 存储层 ──────────────────────────────────────────────────────

class PlatformSettingsStore:
    """platform_settings 表的 DAO(key → value JSON)。

    【线程安全】同 PackSettingsStore 模式:每方法新开连接 + 写锁,
    多线程读 / 单线程写,无跨方法事务。
    """

    def __init__(self, database_url: Optional[str] = None):
        from services.db import get_pg_engine
        self.db = get_pg_engine(database_url)
        self._lock = threading.Lock()  # 写操作串行化(读走池借还,无需锁)
        self._init_db()
        logger.info("PlatformSettingsStore initialized: postgres")

    def _get_conn(self):
        return self.db.connect()

    def _init_db(self):
        """幂等建表(与 pack_settings 同库不同表,互不影响)。"""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS platform_settings (
                    key         TEXT PRIMARY KEY,  -- 设置键(如 "display")
                    value_json  TEXT NOT NULL,     -- JSON: {字段key: 保存值}
                    updated_at  TEXT NOT NULL      -- ISO 时间戳(UTC)
                )
            """)

    def get(self, key: str) -> Optional[Any]:
        """读一个键的 JSON 值(无记录/损坏返回 None)。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value_json FROM platform_settings WHERE key = ?", (key,)
            ).fetchone()
        if not row or not row["value_json"]:
            return None
        try:
            return json.loads(row["value_json"])
        except (ValueError, TypeError):
            logger.warning(f"platform_settings.{key} 的 value_json 损坏,按空处理")
            return None

    def set(self, key: str, value: Any) -> None:
        """整体写一个键的 JSON 值(upsert)。"""
        with self._lock, self._get_conn() as conn:
            conn.execute(
                """INSERT INTO platform_settings (key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                                  updated_at = excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), _now()),
            )


def get_platform_settings_store(request) -> "PlatformSettingsStore":
    """取 app 级 PlatformSettingsStore 单例(懒建并挂 app.state)。

    main.py lifespan 装配后直接命中;最小测试 app(不经 main)首次
    访问时懒建——PG 引擎按 DSN 共享,不会重复开池。
    """
    store = getattr(request.app.state, "platform_settings_store", None)
    if store is None:
        store = PlatformSettingsStore()
        request.app.state.platform_settings_store = store
    return store
