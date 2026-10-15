"""PlatformSettings - 平台级 KV 设置的存储。

【模块定位】
平台自身(非插件)的少量全局配置数据层。与管理端"插件设置"
(services/pack_settings.py,pack 级)区分:这里存的是平台级键值,
当前唯一消费者是「显示设置」(对话界面的链路/检索结果显示开关,
管理端配置、独立/嵌入对话页共读)。

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


# ── 显示设置(display 键的领域逻辑,admin/settings 两路由共用) ──

# platform_settings 表里的键名
DISPLAY_KEY = "display"

# 默认值:两个开关都开(需求原文「默认开启 / 默认显示」)
DISPLAY_DEFAULTS: Dict[str, bool] = {"show_pipeline": True, "show_retrieval": True}


def get_display_settings(store: Optional["PlatformSettingsStore"]) -> Dict[str, bool]:
    """读显示设置:库里没有/字段缺失/类型不对时逐字段回默认值。

    读取永远不抛异常——显示开关挂了不该连累聊天页渲染。
    """
    saved = store.get(DISPLAY_KEY) if store is not None else None
    saved = saved if isinstance(saved, dict) else {}
    return {
        "show_pipeline": saved.get("show_pipeline")
        if isinstance(saved.get("show_pipeline"), bool)
        else DISPLAY_DEFAULTS["show_pipeline"],
        "show_retrieval": saved.get("show_retrieval")
        if isinstance(saved.get("show_retrieval"), bool)
        else DISPLAY_DEFAULTS["show_retrieval"],
    }


def save_display_settings(
    store: "PlatformSettingsStore", payload: Dict[str, Any]
) -> Tuple[Dict[str, bool], List[Dict[str, str]]]:
    """校验并合并保存显示设置(部分更新:只提交要改的键)。

    Returns:
        (merged, errors)。errors 非空时调用方应整体拒绝(不落半截配置)。
        只认两个布尔键,未知键/非布尔值都报错——防止拼写错误的键静默沉库。
    """
    errors: List[Dict[str, str]] = []
    clean: Dict[str, Any] = {}
    for key, raw in (payload or {}).items():
        if key not in DISPLAY_DEFAULTS:
            errors.append({"field": key, "message": "未知配置项(只支持 show_pipeline/show_retrieval)"})
            continue
        if not isinstance(raw, bool):
            errors.append({"field": key, "message": "必须是布尔值(true/false)"})
            continue
        clean[key] = raw
    if errors:
        return get_display_settings(store), errors
    merged = get_display_settings(store)
    merged.update(clean)
    store.set(DISPLAY_KEY, merged)
    return merged, []


# ── 模型单价(model_pricing 键,P2#13 费用观测) ─────────────────

# platform_settings 表里的键名
MODEL_PRICING_KEY = "model_pricing"

# 单价条目只认这两个键(元/千 token);多余键整体拒绝,防拼写错误静默沉库
_PRICING_FIELDS = ("input_per_1k", "output_per_1k")


def get_model_pricing(store: Optional["PlatformSettingsStore"]) -> Dict[str, Any]:
    """读模型单价表:{models: {模型名: {input_per_1k, output_per_1k}}}。

    库里没有/结构损坏时逐层回空——单价缺失只影响费用数字(cost=null),
    不应连累聚合端点报错。返回形状与 PUT 请求体一致(前端免转换)。
    """
    saved = store.get(MODEL_PRICING_KEY) if store is not None else None
    saved = saved if isinstance(saved, dict) else {}
    models = saved.get("models") if isinstance(saved.get("models"), dict) else {}
    clean: Dict[str, Dict[str, float]] = {}
    for name, entry in models.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        values = {}
        for field in _PRICING_FIELDS:
            raw = entry.get(field)
            values[field] = float(raw) if isinstance(raw, (int, float)) \
                and not isinstance(raw, bool) else 0.0
        clean[name] = values
    return {"models": clean}


def save_model_pricing(
    store: "PlatformSettingsStore", payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """校验并整体替换保存模型单价表(PUT 语义:提交即全量)。

    Returns:
        (merged, errors)。errors 非空时调用方应整体拒绝(不落半截配置)。
        校验规则:models 必须是对象;模型名非空字符串;条目只认
        input_per_1k/output_per_1k 两个非负有限数字键。
    """
    import math

    errors: List[Dict[str, str]] = []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict):
        return get_model_pricing(store), [
            {"field": "models", "message": '请求体必须是 {"models": {模型名: {input_per_1k, output_per_1k}}}'}]

    clean: Dict[str, Dict[str, float]] = {}
    for name, entry in models.items():
        if not isinstance(name, str) or not name.strip():
            errors.append({"field": f"models.{name}", "message": "模型名必须是非空字符串"})
            continue
        if not isinstance(entry, dict):
            errors.append({"field": f"models.{name}", "message": "单价条目必须是对象"})
            continue
        if set(entry) != set(_PRICING_FIELDS):
            errors.append({"field": f"models.{name}",
                           "message": "条目只支持 input_per_1k/output_per_1k 两个键(均必填)"})
            continue
        values: Dict[str, float] = {}
        bad = False
        for field in _PRICING_FIELDS:
            raw = entry.get(field)
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw)) or float(raw) < 0):
                errors.append({"field": f"models.{name}.{field}",
                               "message": "必须是 >= 0 的数字(元/千 token)"})
                bad = True
        if bad:
            continue
        clean[name.strip()] = {f: float(entry[f]) for f in _PRICING_FIELDS}

    if errors:
        return get_model_pricing(store), errors
    store.set(MODEL_PRICING_KEY, {"models": clean})
    return get_model_pricing(store), []


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
