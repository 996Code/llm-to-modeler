"""mcp 数据源健康巡检: list_tools 探测, 失败如实报不健康。

check_health 返回 {healthy, latency_ms, server_version|error}(dict,
非 brief 骨架里的二元组——按 datasources.py 实际形状适配)。
mcp 探测走 sdk.mcp_client.list_tools(server code);monkeypatch 打在
模块属性上, chatbi 侧必须以 `mcp_client.list_tools(...)` 模块引用调用。

跑法: cd backend && .venv/bin/python -m pytest tests/domains/test_chatbi_mcp_health.py -v
"""
from domains.chatbi.datasources import DataSourceInfo, check_health


def _mcp_info(code="srv_a"):
    return DataSourceInfo(id="x", name="外部", db_type="mcp",
                          host=code, port=0, database="",
                          username="", password_plain="")


def test_healthy(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "list_tools",
                        lambda code: [{"name": "echo"}])
    h = check_health(_mcp_info())
    assert h["healthy"] is True
    assert h.get("error", "") == ""
    assert "srv_a" in h["server_version"]


def test_unhealthy(monkeypatch):
    from sdk.tool import PackInvokeError
    from sdk import mcp_client

    def boom(code):
        raise PackInvokeError("MCP server 连接失败: timeout",
                              PackInvokeError.RETRYABLE)

    monkeypatch.setattr(mcp_client, "list_tools", boom)
    h = check_health(_mcp_info())
    assert h["healthy"] is False
    assert "srv_a" in h["error"]
