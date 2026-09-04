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
"""
import importlib
import inspect
import logging
import threading
from typing import Any, List, Optional

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


def mount_pack_routers(app: Any, pack_names: List[str]) -> List[str]:
    """为加载成功的 pack 挂载各自的 API router。

    Args:
        app: FastAPI 实例(main lifespan / admin 热切换的 request.app)。
        pack_names: 本次装配成功的 pack 名单(load_all_packs 的结果)。

    Returns:
        实际挂载了 API 的 pack 名列表(无 create_api_router 的 pack 跳过)。
    """
    with _MOUNT_LOCK:
        _unmount_all(app)

        mounted: dict = {}
        for name in sorted(set(pack_names)):
            try:
                module = importlib.import_module(f"domains.{name}.pack")
            except ImportError as e:
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
                # 单 pack 路由构造失败只跳过该 pack,不拖垮整个装配
                # (与 load_all_packs 的逐 pack 容错一致)
                logger.exception(f"pack api mount: {name} 路由构造失败,已跳过")
                continue
            if router is None:
                continue

            prefix_routes_before = set(map(id, app.router.routes))
            app.include_router(router, prefix=f"/api/packs/{name}", tags=[f"pack:{name}"])
            # 记录本次 include 新增的 route 引用(卸载时按引用移除)
            added = [r for r in app.router.routes if id(r) not in prefix_routes_before]
            mounted[name] = added
            logger.info(f"pack api mounted: /api/packs/{name} ({len(added)} routes)")

        setattr(app.state, MOUNTED_ATTR, mounted)
        return sorted(mounted.keys())


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
