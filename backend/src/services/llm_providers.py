"""平台 LLM 供应商配置(设计 8.6/C8):DB 唯一事实源,管理页维护,运行时热生效。

职责边界:
  - 本模块负责配置存取,revision 用于运行时缓存失效;
  - 运行时解析与主备切换在 llm/client.py(每轮读当前生效配置);
  - 管理端 API 在 api/llm_providers.py。

配置形状(content JSON):
  {"primary":   {base_url, api_key, model, enabled, price_in, price_out},
   "fallback":  {...同构,可缺省}}
  - api_key 明文存储(与 .env 同信任域:库与配置文件同机同权限;
    管理端 GET/PUT 均走 admin 门禁,GET 不回显,PUT 留空沿用旧值)。
  - price_in/out: 元/千token,可空(null)——空则调用日志不计成本。
  - enabled=False 的角色不参与解析(留配置不删,便于临时停用);
    fallback 缺省 = 无备用。
"""
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SCOPE = "global"


class ProviderConfigError(ValueError):
    """配置结构非法(管理端保存 400)。"""


# ── 存取(平台 public.llm_providers 单行档,模式对齐 assistant_config)──

def _conn():
    from services.db import get_pg_engine
    return get_pg_engine().connect()


_table_ready = False


def _ensure_table(conn) -> None:
    """幂等建表(进程内只跑一次——DDL 每次执行会取锁,导入高并发下
    成千上万次 LLM 调用各跑一遍 CREATE TABLE 会自相竞争)。"""
    global _table_ready
    if _table_ready:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_providers (
            scope TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            content TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        )""")
    _table_ready = True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_providers() -> Optional[Dict[str, Any]]:
    """读当前配置(无行返回 None)。"""
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT revision, content FROM llm_providers WHERE scope = ?",
            (SCOPE,)).fetchone()
    if not row:
        return None
    content = json.loads(row["content"])
    content["_revision"] = int(row["revision"])
    return content


def current_revision() -> int:
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT revision FROM llm_providers WHERE scope = ?",
            (SCOPE,)).fetchone()
    return int(row["revision"]) if row else 0


_ROLE_FIELDS = ("base_url", "api_key", "model", "enabled", "price_in", "price_out")


def validate_providers(content: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化为可入库配置;非法抛 ProviderConfigError。"""
    if not isinstance(content, dict):
        raise ProviderConfigError("配置必须是 JSON 对象")
    primary = content.get("primary")
    if not isinstance(primary, dict) or not (primary.get("base_url") or "").strip():
        raise ProviderConfigError("primary 缺少 base_url")
    out: Dict[str, Any] = {}
    for role in ("primary", "fallback"):
        cfg = content.get(role)
        if cfg is None:
            if role == "primary":
                raise ProviderConfigError("primary 必填")
            continue  # fallback 可缺省
        if not isinstance(cfg, dict):
            raise ProviderConfigError(f"{role} 必须是对象")
        if not (cfg.get("base_url") or "").strip():
            raise ProviderConfigError(f"{role} 缺少 base_url")
        # 密钥更新语义(常规做法):GET 不回显;PUT 填了才换,留空=沿用旧值
        # (_existing_key 由管理端从库中旧档并入,入库前剔除)
        norm: Dict[str, Any] = {
            "base_url": str(cfg["base_url"]).strip().rstrip("/"),
            "api_key": str(cfg.get("api_key") or cfg.get("_existing_key") or ""),
            "model": str(cfg.get("model") or "").strip(),
            "enabled": bool(cfg.get("enabled", True)),
        }
        for pf in ("price_in", "price_out"):
            v = cfg.get(pf)
            if v in (None, "", 0, "0"):
                norm[pf] = None
            else:
                try:
                    norm[pf] = float(v)
                except (TypeError, ValueError):
                    raise ProviderConfigError(f"{role}.{pf} 必须是数值(元/千token)")
        out[role] = norm
    if out["primary"].get("model") == "":
        raise ProviderConfigError("primary 缺少 model")
    return out


def save_providers(content: Dict[str, Any], updated_by: str = "") -> int:
    """直接保存并递增 revision,使运行时配置缓存失效。"""
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT revision FROM llm_providers WHERE scope = ?",
            (SCOPE,)).fetchone()
        current = int(row["revision"]) if row else 0
        payload = validate_providers(content)
        new_rev = current + 1
        if row:
            conn.execute(
                "UPDATE llm_providers SET revision=?, content=?, updated_by=?, "
                "updated_at=? WHERE scope=?",
                (new_rev, json.dumps(payload, ensure_ascii=False),
                 updated_by or "", _now(), SCOPE))
        else:
            conn.execute(
                "INSERT INTO llm_providers (scope, revision, content, "
                "updated_by, updated_at) VALUES (?,?,?,?,?)",
                (SCOPE, new_rev, json.dumps(payload, ensure_ascii=False),
                 updated_by or "", _now()))
    invalidate_cache()
    return new_rev


# ── 运行时解析缓存(revision 比对,LLMClient 每轮调用走这里)──────────

_cache_lock = threading.Lock()
_cached: Optional[Tuple[int, Dict[str, Any]]] = None


def resolve_active() -> Dict[str, Any]:
    """当前生效的角色配置(缓存+revision 比对;DB 故障回退缓存/空)。

    返回 {"primary": {...明文 key...}, "fallback": {...}|None} —
    enabled=False 或缺省的角色不在结果里。
    """
    global _cached
    with _cache_lock:
        try:
            rev = current_revision()
            if _cached is not None and _cached[0] == rev:
                return _cached[1]
            cfg = load_providers() or {}
            active = {}
            for role in ("primary", "fallback"):
                c = cfg.get(role) or {}
                if c and c.get("enabled", True):
                    active[role] = c
            if "primary" not in active:
                # 未配置/主角色停用 → 空配置(调用方 llm/client 前置闸
                # 报错引导去管理页配置——不回退 env,单一事实源)
                return {}
            _cached = (rev, active)
            return active
        except Exception:  # noqa: BLE001
            logger.exception("llm_providers 解析失败,沿用上次缓存(无缓存视为未配置)")
            return {} if _cached is None else _cached[1]


def invalidate_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


def reset_runtime_cache_for_tests() -> None:
    invalidate_cache()
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM llm_providers WHERE scope = ?", (SCOPE,))
