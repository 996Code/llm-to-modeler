"""用户级设置 API —— 聊天页(独立/嵌入)读取平台配置的入口。

【模块定位】
平台级设置的**读**端点(用户级,无 admin 门):独立对话页与嵌入对话页
共读这一个端点拿「显示设置」(链路/思考信息、检索结果两开关)。

与 /api/admin/settings/display 的分工:
  - 管理端(admin.py,require_admin 门内):GET 读 + PUT 写——配置入口
    只在管理界面,对话界面不支持配置(用户纠正后的需求)
  - 本路由(用户级):只读。走全局 AuthMiddleware(ADMIN_TOKEN 已配置
    时须带 Bearer token,与其它业务 API 同链路),但不要求管理口令

端点清单:
  GET /api/settings/display → 显示设置(默认 {show_pipeline: true, show_retrieval: true})
"""
import logging

from fastapi import APIRouter, Request

from services.platform_settings import get_display_settings, get_platform_settings_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/display")
async def get_user_display_settings(request: Request):
    """读显示设置(用户级,聊天页/嵌入页挂载时调用)。

    库里没有配置时返回默认值(两开关全开)——读取链路永远 200,
    不让配置缺失连累聊天页渲染。
    """
    store = get_platform_settings_store(request)
    return get_display_settings(store)
