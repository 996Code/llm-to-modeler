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
from typing import Optional

from fastapi import Request


async def admin_required(request: Request) -> None:
    """管理端鉴权依赖:与 /api/admin 同一把口令(X-Admin-Token)。

    开放模式(未配置 ADMIN_TOKEN)直接放行——语义与 api.admin.require_admin
    完全一致,只是转发入口在 sdk(pack 侧使用)。
    """
    from api.admin import require_admin  # 延迟导入:避免 sdk → api 顶层依赖
    await require_admin(request)


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
