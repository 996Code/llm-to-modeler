"""PackDependency - 插件基础设施依赖的声明式检测。

【模块定位】
平台层的"插件加载闸门":pack 在 manifest(config.yaml)里声明自己依赖的
外部基础设施(数据库/向量库/...),本模块在 **import pack 之前** 判定依赖
是否满足——不满足则该 pack 不加载(fail-closed),但不影响其他 pack 与
服务启动。插件中心仍能看到它(目录扫描不依赖 import),显示"依赖未配置"
与缺失清单,补配后可热恢复。

【manifest 声明格式】(dependencies 段,块风格)
    dependencies:
      neo4j:
        required: true
        fields:                      # 引用 settings.schema.yaml 的字段 key
          - neo4j_uri                #   (解析链:设置页保存值 > env > 默认)
          - neo4j_user
          - neo4j_password
        probe:                       # 可选连通性探针(配置齐全才执行)
          module: domains.knowledge_graph.probes
          fn: neo4j
      legacy_pack_dep:               # 无 settings schema 的 pack 可直接声明 env 名
        required: true
        env:
          - SOME_API_KEY

【铁律:平台零领域知识】
本模块不认识 neo4j/milvus/任何具体设施。探针有两种表达:
  1. module + fn:pack 自己提供的探针函数(懒加载——只在配置齐全后 import,
     签名 fn(resolved_settings: dict) -> None,异常即失败);
  2. kind: http + url_field:通用 HTTP GET 探针(2xx 即通过)。
平台内置的只有"字段解析 + 声明驱动的探针调度 + 超时 + 缓存"这套机制。

【探针治理】
  - PACK_DEPENDENCY_PROBE=0 关闭启动期探针(只做配置存在性检查,CI/离线
    环境用);默认开启。
  - 探针超时 PACK_DEPENDENCY_PROBE_TIMEOUT 秒(默认 3),在独立线程跑,
    不拖慢启动。
  - 结果缓存 60s(避免管理端列表反复探测);recheck 接口可显式清缓存。
"""
import concurrent.futures
import importlib
import logging
import os
import time
from typing import Any, Dict, List, Optional

from services.pack_settings import (
    PackSettingsStore,
    find_field,
    read_settings_schema,
    resolve_all,
)

logger = logging.getLogger(__name__)

# 状态枚举(前端与管理端共用)
STATUS_OK = "ok"
STATUS_MISSING = "missing_dependency"
STATUS_PROBE_FAILED = "probe_failed"

# 探针结果缓存:{(pack_name, dep_name): (ok, detail, monotonic_ts)}
_PROBE_CACHE: Dict[tuple, tuple] = {}
_PROBE_CACHE_TTL = 60.0

# 探针执行线程池。多线程是必要的:future.result(timeout) 只放弃等待、
# 杀不掉底层线程,单线程池里一个挂死的探针(如被防火墙丢包的 TCP 连接
# 一直挂到驱动自身超时)会占住唯一线程,让排在后面的健康依赖被 3s 超时
# 误判为失败——错误的 fail-closed。容量取"并行评估的全部探针数 + 余量"。
_PROBE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="dep-probe"
)


def probe_enabled() -> bool:
    """启动/装配期是否执行连通性探针(env PACK_DEPENDENCY_PROBE=0 关闭)。"""
    return os.getenv("PACK_DEPENDENCY_PROBE", "1").strip() != "0"


def _probe_timeout() -> float:
    try:
        return float(os.getenv("PACK_DEPENDENCY_PROBE_TIMEOUT", "3"))
    except ValueError:
        return 3.0


def clear_probe_cache(pack_name: Optional[str] = None) -> None:
    """清探针缓存(全部或指定 pack)——"重新检测"按钮的后端入口。"""
    if pack_name is None:
        _PROBE_CACHE.clear()
        return
    for key in [k for k in _PROBE_CACHE if k[0] == pack_name]:
        _PROBE_CACHE.pop(key, None)


def _dependency_fields(dep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把依赖声明归一为"待解析字段"列表。

    fields(引用 schema 字段)与 env(裸环境变量名)都支持;env 名视为
    虚拟字段 {key: 名, env: 名}(解析链里 env 兜底自然命中)。
    """
    fields: List[Dict[str, Any]] = []
    for key in (dep.get("fields") or []):
        fields.append({"key": str(key)})
    for env_name in (dep.get("env") or []):
        fields.append({"key": str(env_name), "env": str(env_name)})
    return fields


def evaluate_pack_dependency(
    pack_name: str,
    dep_name: str,
    dep: Dict[str, Any],
    resolved: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
    use_probe: bool,
) -> Dict[str, Any]:
    """评估单个依赖:字段存在性 → (可选)连通性探针。

    Args:
        resolved: 该 pack 全字段最终值(services.pack_settings.resolve_all)
        schema:   settings.schema.yaml(字段 label / 探针 url_field 用)

    Returns:
        {"status": ok|missing_dependency|probe_failed, "missing": [...], "detail": str}
    """
    fields = _dependency_fields(dep)
    if not fields:
        # 声明了依赖但既无 fields 也无 env:视为声明不完整,按缺失处理
        return {
            "status": STATUS_MISSING,
            "missing": [f"{dep_name}(声明缺少 fields/env)"],
            "detail": "依赖声明不完整",
        }

    missing_labels: List[str] = []
    for f in fields:
        key = f["key"]
        value = resolved.get(key)
        if value is None or str(value).strip() == "":
            label = key
            field_def = find_field(schema, key)
            if field_def and field_def.get("label"):
                label = f"{field_def['label']}({key})"
            missing_labels.append(label)

    if missing_labels:
        return {
            "status": STATUS_MISSING,
            "missing": missing_labels,
            "detail": f"缺少配置: {', '.join(missing_labels)}",
        }

    # 配置齐全 → 可选探针
    probe_spec = dep.get("probe")
    if not use_probe or not probe_enabled() or not probe_spec:
        return {"status": STATUS_OK, "missing": [], "detail": ""}

    cache_key = (pack_name, dep_name)
    cached = _PROBE_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[2]) < _PROBE_CACHE_TTL:
        ok, detail, _ = cached
        return {
            "status": STATUS_OK if ok else STATUS_PROBE_FAILED,
            "missing": [],
            "detail": detail,
        }

    ok, detail = _run_probe(pack_name, dep_name, probe_spec, resolved, schema)
    _PROBE_CACHE[cache_key] = (ok, detail, time.monotonic())
    return {
        "status": STATUS_OK if ok else STATUS_PROBE_FAILED,
        "missing": [],
        "detail": detail,
    }


def _run_probe(
    pack_name: str,
    dep_name: str,
    probe_spec: Dict[str, Any],
    resolved: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
) -> tuple:
    """执行一次探针(带超时),返回 (ok, detail)。任何异常都算探针失败。"""
    try:
        future = _PROBE_EXECUTOR.submit(
            _do_probe, pack_name, dep_name, probe_spec, resolved, schema
        )
        future.result(timeout=_probe_timeout())
        return True, ""
    except concurrent.futures.TimeoutError:
        msg = f"探针超时(>{_probe_timeout():.0f}s)"
        logger.warning(f"[{pack_name}.{dep_name}] {msg}")
        return False, msg
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.warning(f"[{pack_name}.{dep_name}] 探针失败: {msg}")
        return False, msg


def _do_probe(
    pack_name: str,
    dep_name: str,
    probe_spec: Dict[str, Any],
    resolved: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
) -> None:
    """探针实际逻辑(在探针线程里跑;抛异常 = 失败)。

    两种 spec:
      {module: ..., fn: ...}  → importlib 懒加载 pack 提供的探针函数
      {kind: http, url_field} → 通用 HTTP GET(url 取该字段解析值)
    """
    if probe_spec.get("kind") == "http":
        url = str(resolved.get(str(probe_spec.get("url_field") or "")) or "").strip()
        if not url:
            raise ValueError("http 探针缺少 url_field 对应的值")
        import httpx
        resp = httpx.get(url, timeout=_probe_timeout())
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}")
        return

    module_path = probe_spec.get("module")
    fn_name = probe_spec.get("fn")
    if not module_path or not fn_name:
        raise ValueError("探针声明缺少 module/fn")
    module = importlib.import_module(str(module_path))
    fn = getattr(module, str(fn_name), None)
    if not callable(fn):
        raise ValueError(f"探针函数不存在: {module_path}.{fn_name}")
    # 探针拿到 pack 的全部最终配置(含密钥,仅服务端内部使用)
    fn(resolved)


def evaluate_pack(
    pack_name: str,
    manifest: Dict[str, Any],
    settings_store: Optional[PackSettingsStore] = None,
    use_probe: bool = True,
) -> Dict[str, Any]:
    """评估一个 pack 的全部依赖(聚合状态)。

    Returns:
        {"status": 聚合状态, "missing": 全部缺失项, "detail": 拼接说明,
         "dependencies": {dep_name: 单项结果}}
        无依赖声明 → status=ok(绝大多数 pack 不声明依赖,零成本通过)。
    """
    deps = (manifest or {}).get("dependencies") or {}
    if not deps:
        return {"status": STATUS_OK, "missing": [], "detail": "", "dependencies": {}}

    schema = read_settings_schema(pack_name)
    saved = settings_store.get_values(pack_name) if settings_store else {}
    resolved = resolve_all(pack_name, saved=saved, schema=schema)

    per_dep: Dict[str, Any] = {}
    all_missing: List[str] = []
    details: List[str] = []
    status = STATUS_OK
    for dep_name, dep in deps.items():
        if not isinstance(dep, dict):
            continue
        if dep.get("required") is False:
            # 可选依赖:失败只记录,不阻断加载
            result = evaluate_pack_dependency(
                pack_name, str(dep_name), dep, resolved, schema, use_probe
            )
            result["optional"] = True
            per_dep[str(dep_name)] = result
            continue
        result = evaluate_pack_dependency(
            pack_name, str(dep_name), dep, resolved, schema, use_probe
        )
        per_dep[str(dep_name)] = result
        if result["status"] == STATUS_MISSING:
            status = STATUS_MISSING
            all_missing.extend(result["missing"])
            details.append(f"{dep_name}: {result['detail']}")
        elif result["status"] == STATUS_PROBE_FAILED and status == STATUS_OK:
            status = STATUS_PROBE_FAILED
            details.append(f"{dep_name}: {result['detail']}")

    return {
        "status": status,
        "missing": all_missing,
        "detail": "; ".join(details),
        "dependencies": per_dep,
    }


def evaluate_packs(
    manifests: Dict[str, Dict[str, Any]],
    settings_store: Optional[PackSettingsStore] = None,
    use_probe: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """批量评估(管理端插件列表用):{pack_name: evaluate_pack 结果}。"""
    return {
        name: evaluate_pack(name, cfg or {}, settings_store, use_probe)
        for name, cfg in manifests.items()
    }
