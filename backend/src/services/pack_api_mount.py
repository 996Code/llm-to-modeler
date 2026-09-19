"""PackApiMount - pack 自有 HTTP API 的动态挂载/卸载。

【模块定位】
pack.py 可选导出 ``create_api_router() -> APIRouter``(不带前缀),本模块
统一挂到 ``/api/packs/{pack_name}`` 前缀下,并在重装配/disable 时先卸后挂
——冷启动与管理端热切换走同一路径(由 pack_manager.assemble_packs 末尾
调用),与 tools/prompt 的热切换语义完全一致。

【实现要点】
  - FastAPI 的 include_router 是"拷贝 route 对象进 app.router.routes 列表",
    卸载 = 把上次挂载记录的 route 对象从列表里移除(引用相等比较)。
  - 每次全量重挂:先卸掉全部 pack 动态路由,再为本次加载成功的 pack 逐个
    挂载。低频管理操作,路由量个位数,全量重挂成本可忽略,且天然幂等。
  - 禁用的 pack 不挂载(依赖检测未过的 pack 也不会出现在加载名单里,
    它的 API 自然不存在——返回 404,这正是"依赖未配置 → 插件不可用"的
    外在表现之一)。

【三十六审 P1-B: route 展开与提交分离】
  三十五审的两阶段只保护"router 构造失败"; commit 段仍是
  _unmount_all → 逐个 app.include_router——include_router 中途抛错时
  旧 route 已被卸掉(真实注入: old_routes_survive=False)。
  现在 build_staged_routes 在**临时 FastAPI** 上展开全部 route 对象
  (构造/导入/展开的任何失败都发生在触碰真实 app 之前), commit_routes
  只做"一次性列表替换 + 挂载记录替换"——纯赋值, 不可能中途失败留下
  半卸载状态。
"""
import importlib
import inspect
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 挂载记录挂在 app.state 上的属性名:{pack_name: [route 对象, ...]}
MOUNTED_ATTR = "_pack_api_mounted"

# 挂载/卸载互斥锁:两个管理端启停并发交错时,"整体覆盖写"的挂载记录会
# 互相踩踏(后写者覆盖前写者 → 前者的 route 成为 app.router.routes 里
# 不可追溯的孤儿,禁用的 pack API 一直可访问到重启)。
_MOUNT_LOCK = threading.Lock()


def _call_router_factory(factory, app: Any):
    """调用 create_api_router,按签名决定是否传 app。

    不用 try/except TypeError 探测——工厂内部任何真实 TypeError 都会被
    误判成"签名不匹配"再重跑一次并原样上抛,一个 pack 的代码 bug 就能
    拖垮整个装配(与 load_all_packs 的逐 pack 容错策略相悖)。
    """
    try:
        params = [
            p for p in inspect.signature(factory).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        takes_app = bool(params)
    except (ValueError, TypeError):
        takes_app = False
    return factory(app) if takes_app else factory()


def build_staged_routes(app: Any, pack_names: List[str]
                        ) -> Tuple[List[Any], Dict[str, List[Any]]]:
    """Prepare: 构建并展开全部新 route(不触碰真实 app 的任何状态)。

    在临时 FastAPI 实例上逐 pack include_router, 收集展开后的 route
    对象列表与挂载记录。router 构造/import/展开的任何失败都发生在
    这里——真实 app.router.routes 尚未被修改。

    Raises:
        PackConfigurationError: critical pack 的 import/构造失败。
    """
    from fastapi import FastAPI
    from sdk.pack_api import PackConfigurationError, critical_packs

    # 禁用临时 app 的默认路由(openapi.json/docs/redoc)——否则会被
    # 当成 pack route 追加进真实 app, 每次 mount 泄漏 4 条
    staged_app = FastAPI(
        openapi_url=None, docs_url=None, redoc_url=None)
    staged_mounted: Dict[str, List[Any]] = {}
    for name in sorted(set(pack_names)):
        try:
            module = importlib.import_module(f"domains.{name}.pack")
        except ImportError as e:
            if name in critical_packs():
                raise PackConfigurationError(
                    f"chatbi pack 模块导入失败: {e}——critical "
                    f"pack, 终止启动(fail-fast)") from e
            # 加载名单里的 pack 理论上都能 import;真实导入错误要留痕
            # (静默吞会把 pack 的代码问题伪装成"没有 API")
            logger.warning(f"pack api mount: import domains.{name}.pack 失败: {e}")
            continue
        factory = getattr(module, "create_api_router", None)
        if not callable(factory):
            continue
        try:
            router = _call_router_factory(factory, app)
        except Exception as e:
            if name in critical_packs():
                # 三十四审 P1-B: critical pack 的 API 挂载失败
                # 终止启动——此前跳过让 ChatBI API 404 假健康
                raise PackConfigurationError(
                    f"chatbi API 路由构造失败: {e}——critical "
                    f"pack, 终止启动(fail-fast)") from e
            # 单 pack 路由构造失败只跳过该 pack,不拖垮整个装配
            # (与 load_all_packs 的逐 pack 容错一致)
            logger.exception(f"pack api mount: {name} 路由构造失败,已跳过")
            continue
        if router is None:
            continue
        before = set(map(id, staged_app.router.routes))
        staged_app.include_router(
            router, prefix=f"/api/packs/{name}", tags=[f"pack:{name}"])
        staged_mounted[name] = [
            r for r in staged_app.router.routes if id(r) not in before]
    # 展开后的全部 route(顺序 = include 顺序, 与直接挂到真实 app 一致)
    staged_routes: List[Any] = list(staged_app.router.routes)
    return staged_routes, staged_mounted


def commit_routes(app: Any, staged_routes: List[Any],
                  staged_mounted: Dict[str, List[Any]]) -> List[str]:
    """Commit: 一次性替换真实 app 的 pack 路由(纯赋值, 不可中途失败)。

    三十六审 P1-B: 替换 = "旧 pack route 全部移除 + 新 route 一次性
    追加"——用列表拼接构造新列表后整体赋值, 不存在"卸了一半挂了一半"
    的中间态。mounted 记录同步替换。
    """
    with _MOUNT_LOCK:
        mounted = getattr(app.state, MOUNTED_ATTR, None) or {}
        old_routes = [r for routes in mounted.values() for r in routes]
        # 保留非 pack 动态路由(平台静态路由), 移除旧 pack route, 追加新
        app.router.routes = [
            r for r in app.router.routes if r not in old_routes
        ] + list(staged_routes)
        setattr(app.state, MOUNTED_ATTR, dict(staged_mounted))
        for name in sorted(staged_mounted):
            logger.info(
                f"pack api mounted: /api/packs/{name} "
                f"({len(staged_mounted[name])} routes)")
        return sorted(staged_mounted.keys())


def mount_pack_routers(app: Any, pack_names: List[str]) -> List[str]:
    """为加载成功的 pack 挂载各自的 API router(两阶段: 先构建后切换).

    Args:
        app: FastAPI 实例(main lifespan / admin 热切换的 request.app)。
        pack_names: 本次装配成功的 pack 名单(load_all_packs 的结果)。

    Returns:
        实际挂载了 API 的 pack 名列表(无 create_api_router 的 pack 跳过)。

    Raises:
        PackConfigurationError: 名单内 critical pack(chatbi)的模块
        import 或路由构造失败——三十四审 P1-B: 此前 catch-continue
        让服务在没有 ChatBI API 的情况下 ready(404 假健康)。

    三十五审 P1-B(两阶段) + 三十六审 P1-B(route 提交原子化):
    build_staged_routes 在临时 app 上展开(失败发生在卸旧之前);
    commit_routes 只做一次性列表替换(include_router 的任何失败
    都不可能发生在真实 app 上)。
    """
    staged_routes, staged_mounted = build_staged_routes(app, pack_names)
    return commit_routes(app, staged_routes, staged_mounted)


def unmount_pack_routers(app: Any, pack_name: Optional[str] = None) -> None:
    """卸载 pack 动态路由(全部或指定 pack)。测试与显式清理用。"""
    with _MOUNT_LOCK:
        if pack_name is None:
            _unmount_all(app)
            return
        mounted = getattr(app.state, MOUNTED_ATTR, None) or {}
        routes = mounted.pop(pack_name, [])
        if routes:
            app.router.routes = [r for r in app.router.routes if r not in routes]
            setattr(app.state, MOUNTED_ATTR, mounted)
            logger.info(f"pack api unmounted: /api/packs/{pack_name}")


def mounted_packs(app: Any) -> List[str]:
    """当前挂载了 API 的 pack 名列表(诊断用)。"""
    return sorted((getattr(app.state, MOUNTED_ATTR, None) or {}).keys())


def _unmount_all(app: Any) -> None:
    """卸载全部 pack 动态路由(重挂前的清理步骤)。"""
    mounted = getattr(app.state, MOUNTED_ATTR, None) or {}
    if mounted:
        all_routes = [r for routes in mounted.values() for r in routes]
        app.router.routes = [r for r in app.router.routes if r not in all_routes]
        logger.info(f"pack api unmounted all: {sorted(mounted)}")
    setattr(app.state, MOUNTED_ATTR, {})
