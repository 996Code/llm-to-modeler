#!/usr/bin/env python
"""南京院客服 Demo 常驻 MCP server(默认 9771)——模拟"计量业务平台"。

工具 query_progress(业务信封返回):
  BJ2026  正常(检测中, 3/5, 预计 2026-10-30)
  BJ2027  正常(已完成, 报告号 RPT-2026-0888)——换单演示
  BJ8888  forbidden(单存在但不属于当前主体)——拒权演示
  BJ9000  not_found(单不存在)——查无演示
  BJ0000  60s 睡眠 → 注册表 read_timeout(10s)截断——故障如实报错演示

工具 query_instrument(器具价目,业务信封返回):
  夹具 metrology_instruments.json(24 条,源自院方提供的价目表):
  测量范围/准确度/检测费/强检一二级目录(null=不在强检目录)/检校
  周期/技术规程;
  名称精确→包含匹配,可叠加型号过滤;"压力"命中多行、"无线温度记录器"
  同名两行(不同量程不同价)可做多行与过滤测试样例。

工具 query_turnaround(器具检测时限,业务信封返回):
  常规/加急工作日(测试数据,演示评分"检测所需工作日"的数据面);
  名称精确→包含匹配;压力表 5/2、电子秤 3/1、角膜曲率计 7/3 等。

工具 query_appointment(预约送检单,date 槽位链路):
  2026-10-26 三条(上午两条/下午一条)、2026-10-27 一条;其余日期 not_found。

工具 query_certificate(证书清单,enum 白名单链路):
  state ∈ {有效, 已过期, 待领取};按状态过滤,空结果 not_found。

工具 query_report(报告进度,通用表格卡 + 拒权分态):
  RPT-2026-0888 已归档 / RPT-2026-0901 在检定中;
  RPT-2025-1204 forbidden(属于其他主体)——通用卡链路的拒权样例。

用法(本机开发): cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py
容器部署(compose metrology-mcp 服务): 复用 app 镜像,entrypoint 覆盖为
  python /app/mcp-e2e/metrology_server.py(command 只覆盖 CMD 不覆盖
  ENTRYPOINT,实测教训),经 METROLOGY_MCP_HOST=0.0.0.0
  监听容器网络(同 compose 网络内 app 经服务名访问);
  注册表 URL 相应配 http://metrology-mcp:9771/mcp。
地址可配: METROLOGY_MCP_HOST(默认 127.0.0.1)/METROLOGY_MCP_PORT(默认 9771)。
停止(本机): lsof -ti :9771 | xargs kill
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from fake_servers import build_metrology_server

if __name__ == "__main__":
    host = os.getenv("METROLOGY_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("METROLOGY_MCP_PORT", "9771"))
    print(f"计量业务平台 MCP server: http://{host}:{port}/mcp "
          "(工具: query_progress / query_instrument / query_turnaround / "
          "query_appointment / query_certificate / query_report)")
    print("单号夹具: BJ2026/BJ2027 正常, BJ8888 拒权, BJ9000 不存在, "
          "BJ0000 超时")
    print("价目夹具: 24 条器具(测量范围/准确度/检测费/强检目录/周期/规程), "
          "名称模糊匹配")
    print("预约夹具: 2026-10-26 三条 / 2026-10-27 一条; "
          "证书夹具: 有效/已过期/待领取; 报告夹具: 0888 归档/0901 在检/"
          "RPT-2025-1204 拒权")
    uvicorn.run(build_metrology_server(), host=host, port=port,
                log_level="warning")
