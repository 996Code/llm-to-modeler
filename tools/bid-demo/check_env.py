#!/usr/bin/env python
"""南京院客服 Demo 演示前环境检查(只读,不改任何状态)。

对照演示脚本(doc/private/bid-demo-script.md §4)检查清单,逐项输出 ✓/✗,
全部通过退出码 0;有 ✗ 按提示排查后重跑:

  1. 计量假 server(mock 业务平台)端口在监听(默认 9771)
  2. 后端 /api/health 正常(默认 18080)
  3. 前端 /ai-modeler/ 可访问(默认 13081)
  4. .env 已配 METROLOGY_PROGRESS_DATASOURCE_ID(读取顺序同 main.py:项目根优先)
  5. MCP server code=metrology:connected+enabled,快照含 query_progress/
     query_instrument,timeout 已调短(超时演示靠注册表 timeout,默认 300s 会拖满)
  6. 知识库「强检目录」「检定规程」存在,且各有文档导入 succeeded
  7. .env 指向的进度数据源存在且 active/mcp/metrology/限定 query_progress;
     价目数据源「计量器具价目」存在且限定 query_instrument

用法: cd backend && .venv/bin/python ../tools/bid-demo/check_env.py \
          [--base http://localhost:18080] [--mock-port 9771] \
          [--frontend http://localhost:13081]
"""
import argparse
import os
import socket
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
CATALOG_KB = "强检目录"
REGULATION_KB = "检定规程"
DS_ENV_KEY = "METROLOGY_PROGRESS_DATASOURCE_ID"
SERVER_CODE = "metrology"
PROGRESS_TOOL = "query_progress"
INSTRUMENT_TOOL = "query_instrument"
FEE_DS_NAME = "计量器具价目"

_failed = 0


def ok(name, cond, evidence="", hint=""):
    """evidence 常态展示;hint 仅失败时展示(排障指引,通过时是噪音)。"""
    global _failed
    cond = bool(cond)
    _failed += 0 if cond else 1
    mark = "✓" if cond else "✗"
    tail = evidence if cond else (hint or evidence)
    print(f"{mark} {name}" + (f" — {tail}" if tail else ""))
    return cond


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _read_env_var(key: str) -> str:
    """与 main.py 的 load_dotenv 语义对齐(override=False):
    真实环境变量优先于 .env;文件选择同序(项目根优先,backend/.env 兜底);
    dotenv_values 处理重复键(后者生效)与行内注释。"""
    val = os.environ.get(key)
    if val:
        return val.strip()
    root_env = HERE.parent.parent / ".env"
    env_path = root_env if root_env.exists() else HERE.parent.parent / "backend" / ".env"
    if not env_path.exists():
        return ""
    return str(dotenv_values(env_path).get(key) or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--mock-port", type=int, default=9771)
    ap.add_argument("--frontend", default="http://localhost:13081")
    args = ap.parse_args()
    client = requests.Session()

    # 1. 计量假 server(只探端口;进程拉起见 tools/mcp-e2e/metrology_server.py)
    ok(f"计量假 server 在跑(:{args.mock_port})", _port_listening(args.mock_port),
       hint="cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py")

    # 2. 后端
    backend_up = False
    try:
        r = client.get(f"{args.base}/api/health", timeout=3)
        backend_up = r.status_code == 200
        ok("后端在跑且健康(/api/health)", backend_up, f"HTTP {r.status_code}")
    except Exception as e:
        ok("后端在跑且健康(/api/health)", False, str(e)[:120])

    # 3. 前端
    try:
        r = client.get(f"{args.frontend}/ai-modeler/", timeout=3)
        ok("前端可访问(/ai-modeler/)", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        ok("前端可访问(/ai-modeler/)", False, str(e)[:120])

    # 4. .env 数据源 ID(读取顺序同后端 main.py)
    ds_id = _read_env_var(DS_ENV_KEY)
    ok(f".env 已配 {DS_ENV_KEY}", bool(ds_id),
       ds_id[:8] + "…" if ds_id else "",
       hint="见 tools/bid-demo/setup_demo.py 收尾输出,配后重启后端")

    if not backend_up:
        print("\n后端不可用,5-7 项跳过;先起后端再重跑本检查。")
        return 1

    # 5. MCP server 注册状态
    try:
        servers = client.get(f"{args.base}/api/admin/mcp/servers", timeout=5).json()
        items = servers if isinstance(servers, list) else (servers.get("items") or [])
        server = next((s for s in items if s.get("code") == SERVER_CODE), None)
        if ok(f"MCP server {SERVER_CODE} 已注册", server is not None):
            ok(f"  状态 connected+enabled",
               server.get("status") == "connected" and server.get("enabled"),
               f"status={server.get('status')} enabled={server.get('enabled')}")
            tools = [t.get("name") for t in (server.get("tools_snapshot") or [])]
            for need in (PROGRESS_TOOL, INSTRUMENT_TOOL):
                ok(f"  快照含 {need}", need in tools, f"工具: {tools}")
            t = float(server.get("timeout") or 0)
            rt = float(server.get("read_timeout") or 0)
            # BJ0000 超时演示靠 read_timeout 截断(默认 300s 会拖满 32s+);
            # 两个都要 >0 且 ≤10(mcp_client 把 0 当默认 30 用)
            ok("  timeout/read_timeout 已调短(1~10s,超时演示依赖)",
               0 < t <= 10.0 and 0 < rt <= 10.0,
               f"timeout={t:g}s read_timeout={rt:g}s",
               hint='PUT /api/admin/mcp/servers/<该server的id> '
                    '{"timeout": 10, "read_timeout": 10} 或重跑 setup_demo.py')
    except Exception as e:
        ok(f"MCP server {SERVER_CODE} 检查", False, str(e)[:120])

    # 6. 知识库两库 + 导入状态
    try:
        kbs = client.get(f"{args.base}/api/packs/knowledge_graph/kbs", timeout=5).json()
        items = kbs if isinstance(kbs, list) else (kbs.get("items") or [])
        by_name = {kb.get("name"): kb for kb in items}
        for name in (CATALOG_KB, REGULATION_KB):
            kb = by_name.get(name)
            if not ok(f"知识库「{name}」存在", kb is not None,
                      hint="跑 tools/bid-demo/setup_demo.py"):
                continue
            docs = client.get(
                f"{args.base}/api/packs/knowledge_graph/kbs/{kb['id']}/documents",
                timeout=5).json().get("items") or []
            succeeded = [d for d in docs if d.get("importStatus") == "succeeded"]
            ok(f"  有导入完成的文档", bool(succeeded),
               f"{len(succeeded)}/{len(docs)} succeeded — " + ", ".join(
                   d.get("filename") or d.get("id", "")[:8] for d in succeeded[:3]))
    except Exception as e:
        ok("知识库检查", False, str(e)[:120])

    # 7. 数据源与 .env 一致 + 价目数据源
    try:
        dss = client.get(f"{args.base}/api/packs/chatbi/datasources", timeout=5).json()
        items = dss.get("items") or dss
        ds = next((d for d in items if d.get("id") == ds_id), None)
        if ok("数据源存在(.env 指向)", ds is not None,
              ds.get("name") if ds else f"id={ds_id}"):
            ok("  active+MCP+metrology+限定工具",
               ds.get("isActive") and ds.get("dbType") == "mcp"
               and ds.get("host") == SERVER_CODE
               and ds.get("database") == PROGRESS_TOOL,
               f"{ds.get('dbType')}/{ds.get('host')}/{ds.get('database')}"
               f"/{'active' if ds.get('isActive') else 'inactive'}")
        fee = next((d for d in items if d.get("name") == FEE_DS_NAME), None)
        if ok(f"价目数据源「{FEE_DS_NAME}」存在", fee is not None,
              hint="重跑 tools/bid-demo/setup_demo.py"):
            ok("  active+MCP+metrology+限定工具",
               fee.get("isActive") and fee.get("dbType") == "mcp"
               and fee.get("host") == SERVER_CODE
               and fee.get("database") == INSTRUMENT_TOOL,
               f"{fee.get('dbType')}/{fee.get('host')}/{fee.get('database')}"
               f"/{'active' if fee.get('isActive') else 'inactive'}")
    except Exception as e:
        ok("数据源检查", False, str(e)[:120])

    print()
    if _failed == 0:
        print("演示环境就绪。入口:")
        print(f"  {args.frontend}/ai-modeler/?packs=metrology_assistant")
        print("(演示浏览器建议先清掉左侧无关历史对话)")
        return 0
    print(f"有 {_failed} 项 ✗,按上方提示排查后重跑。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
