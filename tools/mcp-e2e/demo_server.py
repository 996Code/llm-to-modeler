#!/usr/bin/env python
"""本地演示用 MCP server(常驻,固定端口 9770)——模拟"外部订单系统"。

提供两个数据查询工具(返回 tabular 数据,ChatBI mcp 数据源可直接消费):
  query_orders  各城市订单量(可按城市过滤/限制行数)
  query_gmv     各城市 GMV 与环比

用法: cd backend && .venv/bin/python ../tools/mcp-e2e/demo_server.py
停止: lsof -ti :9770 | xargs kill
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from fake_servers import build_report_server

if __name__ == "__main__":
    print("演示 MCP server: http://127.0.0.1:9770/mcp (工具: query_orders / query_gmv)")
    uvicorn.run(build_report_server(), host="127.0.0.1", port=9770,
                log_level="warning")
