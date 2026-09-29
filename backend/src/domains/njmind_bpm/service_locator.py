"""BPM 上游服务定位器 —— pack 内工具获取 BpmModelerAPI 的唯一入口。

ToolContext 不携带 UpstreamClient(form 走 enhance_asset_client 注入
asset_client;BPM 的 parse/render 是 pack 专有端点,不进通用 adapter)。
装配期 pack.py 的 wire_transport() 由 main/pack_manager 注入 transport
(与 enhance_asset_client 同一装配点);测试可 reset_bpm_transport() 后
set 任意 fake。
"""
from typing import Optional

from domains.njmind_bpm.upstream import BpmModelerAPI

_transport = None
_api: Optional[BpmModelerAPI] = None


def wire_transport(transport) -> None:
    """装配期注入通用传输(UpstreamClient 或测试 fake)。"""
    global _transport, _api
    _transport = transport
    _api = BpmModelerAPI(transport)


def get_api() -> BpmModelerAPI:
    """取领域 API。未装配 → RuntimeError(编程错误,尽早暴露)。"""
    if _api is None:
        raise RuntimeError(
            "njmind_bpm transport 未装配(wire_transport 未被调用);"
            "正常路径由 main.py lifespan / pack_manager 装配期注入")
    return _api


def reset_bpm_transport() -> None:
    """测试隔离用。"""
    global _transport, _api
    _transport = None
    _api = None


def set_api_for_testing(api) -> None:
    """测试专用:直接注入 fake BpmModelerAPI(免 transport)。"""
    global _api
    _api = api
