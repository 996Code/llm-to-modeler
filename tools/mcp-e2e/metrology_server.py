#!/usr/bin/env python
"""南京院客服 Demo 常驻 MCP server(固定端口 9771)——模拟"计量业务平台"。

工具 query_progress(业务信封返回):
  BJ2026  正常(检测中, 3/5, 预计 2026-10-30)
  BJ2027  正常(已完成, 报告号 RPT-2026-0888)——换单演示
  BJ8888  forbidden(单存在但不属于当前主体)——拒权演示
  BJ9000  not_found(单不存在)——查无演示
  BJ0000  60s 睡眠 → 客户端 30s 超时——故障如实报错演示

工具 query_instrument(器具价目,业务信封返回):
  夹具 metrology_instruments.json(24 条,源自院方提供的价目表):
  检测费/强检一二级目录(null=不在强检目录)/检校周期/技术规程;
  名称精确→包含匹配,可叠加型号过滤;"压力"命中多行、"无线温度记录器"
  同名两行(不同量程不同价)可做多行与过滤测试样例。

用法: cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py
停止: lsof -ti :9771 | xargs kill
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from fake_servers import build_metrology_server

if __name__ == "__main__":
    print("计量业务平台 MCP server: http://127.0.0.1:9771/mcp "
          "(工具: query_progress / query_instrument)")
    print("单号夹具: BJ2026/BJ2027 正常, BJ8888 拒权, BJ9000 不存在, BJ0000 超时")
    print("价目夹具: 24 条器具(检测费/强检目录/周期/规程), 名称模糊匹配")
    uvicorn.run(build_metrology_server(), host="127.0.0.1", port=9771,
                log_level="warning")
