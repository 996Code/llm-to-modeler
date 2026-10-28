#!/usr/bin/env python
"""设备管理平台 mock MCP server(默认 9772)——第二业务域。

与 metrology_server.py 同款骨架(复用 fake_servers.build_equipment_server)。
用途:验证多 MCP 服务的注册/发现/分组展示——注册 code=equipment 后,
其工具自动进入智能客服路由(需有绑定该 server 的数据源),能力路由页
按服务分组呈现并显示声明质量画像。

用法(本机开发): cd backend && .venv/bin/python ../tools/mcp-e2e/equipment_server.py
容器部署: 参照 metrology-mcp 的 entrypoint 覆盖方式,本文件随 tools/mcp-e2e
整体复制;地址可配 EQUIPMENT_MCP_HOST/PORT。
"""
import os

import uvicorn

from fake_servers import build_equipment_server

if __name__ == "__main__":
    host = os.getenv("EQUIPMENT_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("EQUIPMENT_MCP_PORT", "9772"))
    print(f"设备管理平台 MCP server: http://{host}:{port}/mcp "
          "(工具: query_equipment_status / query_equipment_list / "
          "query_calibration_due)")
    uvicorn.run(build_equipment_server(), host=host, port=port,
                log_level="warning")
