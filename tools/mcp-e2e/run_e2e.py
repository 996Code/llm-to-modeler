#!/usr/bin/env python
"""MCP 双向端到端验证——注册 / 消费 / 对外全链路一键跑。

用法（backend venv）:
    cd backend && .venv/bin/python ../tools/mcp-e2e/run_e2e.py \
        --base http://localhost:18080 \
        --frontend-origin http://localhost:13081 \
        --api-key sk-verify-mcp-1

前置: 后端/前端 dev server 在跑; 管理端已配置对外 MCP key(或 --api-key 传入)。
排障: ask_data 步失败时查 call_logs 取证:
    SELECT stage, request_data FROM call_logs ORDER BY created_at DESC LIMIT 5;
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from fake_servers import (GMV_WAN, ORDERS, REPORT_AUTH_KEY,
                          build_auth_server, build_report_server,
                          build_sse_server, start_server)

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, evidence: str = ""):
    RESULTS.append((name, ok, evidence))
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {evidence}" if evidence else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--frontend-origin", default="http://localhost:13081")
    ap.add_argument("--api-key", default="",
                  help="对外 MCP key(管理端 MCP 服务页查看/刷新;必填)")
    ap.add_argument("--question", default="查一下各城市订单量对比")
    args = ap.parse_args()
    if not args.api_key:
        sys.exit("缺少 --api-key:管理端「插件→MCP 服务」页查看当前 Key")

    # ── 1. 起异构 fake servers ─────────────────────────────────
    port_report, srv_report = start_server(build_report_server())
    port_sse, srv_sse = start_server(build_sse_server())
    port_auth, srv_auth = start_server(build_auth_server())
    step("起 3 个异构 fake MCP server",
         all([srv_report.started, srv_sse.started, srv_auth.started]),
         f"report:{port_report} sse:{port_sse} auth:{port_auth}")

    created_server_ids, created_ds_ids = [], []
    client = httpx.Client(timeout=60)
    try:
        # ── 2. 注册(进方向) ────────────────────────────────────
        for name, code, url, headers in [
            ("E2E 报表数据", "e2e_report",
             f"http://127.0.0.1:{port_report}/mcp", {}),
            ("E2E SSE", "e2e_sse",
             f"http://127.0.0.1:{port_sse}/sse", {}),
            ("E2E 鉴权", "e2e_auth",
             f"http://127.0.0.1:{port_auth}/mcp",
             {"X-Api-Key": REPORT_AUTH_KEY}),
        ]:
            r = client.post(f"{args.base}/api/admin/mcp/servers",
                            json={"name": name, "code": code, "url": url,
                                  "headers": headers})
            body = r.json()
            created_server_ids.append(body.get("id"))
            step(f"注册 {code}", r.status_code == 200
                 and body.get("status") == "connected",
                 f"工具数 {len(body.get('tools_snapshot') or [])}")
        r = client.post(f"{args.base}/api/admin/mcp/servers", json={
            "name": "E2E 坏地址", "code": "e2e_bad",
            "url": "http://127.0.0.1:1/mcp", "headers": {}})
        created_server_ids.append(r.json().get("id"))
        step("坏 URL 注册不阻塞(待连接)",
             r.status_code == 200 and r.json().get("status") == "pending")

        # ── 3. 建 mcp 数据源 ───────────────────────────────────
        r = client.post(f"{args.base}/api/packs/chatbi/datasources", json={
            "name": "E2E 外部报表", "db_type": "mcp",
            "host": "e2e_report", "port": 0,
            "database": "", "username": "", "password": ""})
        ds_id = r.json().get("id")
        created_ds_ids.append(ds_id)
        step("建 mcp 数据源", r.status_code == 200 and bool(ds_id),
             f"id={ds_id}")

        # ── 4. 扫描合成语义层 ──────────────────────────────────
        r = client.post(f"{args.base}/api/packs/chatbi/datasources/{ds_id}/scan")
        step("触发扫描", r.status_code == 200)
        for _ in range(120):
            s = client.get(
                f"{args.base}/api/packs/chatbi/datasources/{ds_id}/scan").json()
            if s.get("scanStatus") in ("done", "failed"):
                break
            time.sleep(1)
        step("扫描完成", s.get("scanStatus") == "done",
             f"status={s.get('scanStatus')} {s.get('scanStage', '')}")

        # ── 5. 消费: ask_data 全链路(真实 LLM) ─────────────────
        ok, evidence = ask_data_round(client, args, ds_id)
        step(f"ask_data 消费({args.question})", ok, evidence)

        # ── 6. 出方向: 官方 client 连前缀网关端点 ──────────────
        ok, evidence = outbound_round(args)
        step("对外端点(前缀网关) tools/list+调用", ok, evidence)

        # ── 7. 错误路径: server 宕机 → 单源健康探测如实报不健康 ──
        # (check_all_health 需连续 3 败才自动禁用——单轮只计数,故测单源端点)
        srv_report.should_exit = True
        time.sleep(1)
        r = client.post(f"{args.base}/api/packs/chatbi/datasources/{ds_id}/health")
        h = r.json()
        step("server 宕机→健康探测如实报不健康",
             r.status_code == 200 and not h.get("healthy", True),
             f"healthy={h.get('healthy')} error={str(h.get('error', ''))[:60]}")
    finally:
        # ── 8. 清理(保留既有数据) ──────────────────────────────
        for ds_id in created_ds_ids:
            if ds_id:
                client.delete(f"{args.base}/api/packs/chatbi/datasources/{ds_id}")
        for sid in created_server_ids:
            if sid:
                client.delete(f"{args.base}/api/admin/mcp/servers/{sid}")
        for srv in (srv_report, srv_sse, srv_auth):
            srv.should_exit = True
        client.close()
        step("清理测试数据", True)

    print("\n==== 汇总 ====")
    fails = [n for n, ok, _ in RESULTS if not ok]
    for n, ok, ev in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    if fails:
        print(f"\n{len(fails)} 项失败。ask_data 失败排障:\n"
              "  SELECT stage, request_data FROM call_logs "
              "ORDER BY created_at DESC LIMIT 5;")
        return 1
    print(f"\n全部 {len(RESULTS)} 项通过。")
    return 0


def ask_data_round(client, args, ds_id) -> tuple[bool, str]:
    """POST /api/config/chat(packs=["chatbi"]) 收 SSE 至 result。

    SSE 事件类型在 `event:` 行,data 是纯 payload(无 type 字段)。
    """
    try:
        with client.stream(
                "POST", f"{args.base}/api/config/chat",
                json={"packs": ["chatbi"], "message": args.question,
                      "pack_params": {"chatbi": {"datasource_id": ds_id}}}) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            result, reply, event_name = None, "", ""
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except (ValueError, TypeError):
                    continue
                if event_name == "result":
                    result = payload
                elif event_name == "reply":
                    reply = str(payload.get("content", ""))[:80]
                elif event_name == "error":
                    return False, f"error 事件: {str(payload)[:200]}"
            if result is None:
                return False, f"未收到 result 事件(reply 尾: {reply})"
            if result.get("needsClarification"):
                return False, ("检索未匹配到工具(追问): "
                               + str(result.get("summary", ""))[:120])
            # result 事件载荷: {"artifactType":"data","data":{sql,columns,
            # rows_sample,rowcount,chart_option,...}}
            data = result.get("data") or {}
            n = int(data.get("rowcount") or 0)
            chart = data.get("chart_option") or {}
            ok = n > 0 or bool(chart)
            return ok, (f"行数={n} 图表={bool(chart)} "
                        f"调用={str(data.get('sql'))[:60]}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def outbound_round(args) -> tuple[bool, str]:
    """官方 mcp client 经前缀网关连 /ai-modeler/api/mcp。"""
    import asyncio
    import httpx as _hx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"{args.frontend_origin}/ai-modeler/api/mcp"

    async def run():
        async with _hx.AsyncClient(
                headers={"X-Api-Key": args.api_key}, timeout=60) as hc:
            async with streamable_http_client(url, http_client=hc) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    names = [t.name for t in tools.tools]
                    if not names:
                        return False, "tools/list 为空"
                    res = await s.call_tool("kb_search",
                                            {"user_input": "测试检索"})
                    return (not res.isError), f"tools={names}"
    try:
        return asyncio.run(run())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    sys.exit(main())
