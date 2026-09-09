"""pack API 层的可复用鉴权/工具 helper(供 domains/*/api.py 使用)。

【为什么放 sdk】
pack 的 api.py 需要两类鉴权语义:管理端(X-Admin-Token)与用户级
(X-User-Id 透传)。require_admin 本体住在 api/admin.py;sdk 不在 import
期反向依赖 api 层(维持 api → domains → sdk 单向),所以这里用函数内
延迟 import 转发——调用时机在请求期,此时 api.admin 必然已加载。

【用法】(pack 的 router 里)
    from sdk.pack_api import admin_required

    router = APIRouter()

    @router.post("/kbs", dependencies=[Depends(admin_required)])
    async def create_kb(...): ...

    @router.post("/search")   # 用户级:身份由 X-User-Id 透传,读法见 user_id()
    async def search(...): ...
"""
import logging
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# ── 管理端鉴权(依赖倒置)─────────────────────────────────────
# SDK 定义鉴权契约,宿主装配期注入实现(main.py 启动时注册
# api.admin.require_admin)。pack 经 sdk.pack_api.admin_required
# 使用——SDK 不反向 import 宿主 api 层。
_admin_auth_fn: Optional[Callable[[Request], Awaitable[None]]] = None


def register_admin_auth(fn: Callable[[Request], Awaitable[None]]) -> None:
    """宿主注册管理端鉴权实现(装配期调用一次)。

    重复注册(漏用幂等保护)记 warning 后覆盖——测试组合根与生产
    组合根先后加载时可见,避免静默串实现。
    """
    global _admin_auth_fn
    if _admin_auth_fn is not None and _admin_auth_fn is not fn:
        logger.warning("register_admin_auth 重复注册(覆盖旧实现)")
    _admin_auth_fn = fn


async def admin_required(request: Request) -> None:
    """管理端鉴权依赖:转发到宿主注册的鉴权实现。

    未注册(宿主装配缺漏)时 503 fail-closed——比静默放行安全:
    管理端点宁可拒绝服务也不能无鉴权裸奔。
    """
    if _admin_auth_fn is None:
        raise HTTPException(503, "管理端鉴权未装配(宿主未注册)")
    await _admin_auth_fn(request)


# ── 插件配置读取器(依赖倒置)─────────────────────────────────
# pack 运行时要读自己的声明式配置(设置页保存值 > env > schema 默认)。
# 读取器由平台实现,SDK 只持有工厂契约——插件经 settings_reader()
# 取用,不 import 平台 services 层。
_settings_reader_factory: Optional[Callable[[str, Any], Any]] = None


def register_settings_reader(factory: Callable[[str, Any], Any]) -> None:
    """宿主注册读取器工厂 factory(pack_name, settings_store) -> reader。

    reader 需实现 get(key, default) / all()(鸭子协议)。
    重复注册记 warning 后覆盖(语义同 register_admin_auth)。
    """
    global _settings_reader_factory
    if _settings_reader_factory is not None and _settings_reader_factory is not factory:
        logger.warning("register_settings_reader 重复注册(覆盖旧实现)")
    _settings_reader_factory = factory


def settings_reader(app_state: Any, pack_name: str) -> Any:
    """取本 pack 的配置读取器(get/all 鸭子协议)。

    未注册工厂(装配缺漏)时返回空读取器:所有键走调用方 default,
    插件以默认配置运行(fail-open——配置读不到不该崩掉整条业务链)。
    """
    if _settings_reader_factory is None:
        class _EmptyReader:
            def get(self, key, default=None):
                return default

            def all(self):
                return {}

        return _EmptyReader()
    return _settings_reader_factory(pack_name, getattr(app_state, "settings_store", None))


# ── 任务提交契约异常 ────────────────────────────────────────

class DuplicateTaskError(RuntimeError):
    """重复任务被拒(submit 的 dedupe_key 已有活任务)。

    定义在 SDK(任务提交是插件经 app_state.task_manager 发起的通用
    操作,异常属插件可见契约);平台 TaskManager 抛出本类型。"""



def user_id(request: Request, default: str = "anonymous") -> str:
    """读取请求用户身份(宿主网关注入的 X-User-Id,缺省 anonymous)。

    与主对话链路(conversations/config API)同一约定。
    """
    return (request.headers.get("X-User-Id") or "").strip() or default


def task_conv_id(task_id: str) -> str:
    """任务内 LLM 调用的 conv_id 约定值:``task:{task_id}``。

    知识图谱导入等任务会调 LLM,把 conv_id 记成本格式后,现有调用日志
    界面(/api/admin/call-logs?convId=task:xxx)即可按"会话"过滤追溯
    导入期的模型消耗,零 schema 改动。
    """
    return f"task:{task_id}"
