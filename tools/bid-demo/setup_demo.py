#!/usr/bin/env python
"""南京院客服 Demo 环境一键准备(幂等: 已存在的注册/库/文档/数据源复用)。

前置:
  1. 计量假 server 在跑:  cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py
  2. 后端 dev server 在跑(默认 http://localhost:18080, .env 已配 LLM/DATABASE_URL)

做六件事:
  1. 注册 MCP server code=metrology(已注册则复用,校验 query_progress/
     query_instrument 在快照)
  2. 建知识库「强检目录」「检定规程」(已存在则复用)
  3. 上传两份演示文档(同库同内容查重跳过;上传即导入)
  4. 等导入完成(importStatus=succeeded;导入走真实 LLM 抽取)
  5. 建 ChatBI mcp 数据源「计量业务平台」(限定 query_progress,已存在复用)
  6. 建 ChatBI mcp 数据源「计量器具价目」(限定 query_instrument,已存在复用)

用法: cd backend && .venv/bin/python ../tools/bid-demo/setup_demo.py \
          [--base http://localhost:18080] [--server-url http://127.0.0.1:9771/mcp]

收尾输出 METROLOGY_PROGRESS_DATASOURCE_ID=<id>: 配进 .env 或启动 env 后
重启后端,Demo 即就绪。
"""
import argparse
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CATALOG_KB = "强检目录"
REGULATION_KB = "检定规程"
SERVER_CODE = "metrology"
# 数据源: (名称, 限定工具)——"场景→datasource+固定工具"绑定的最小形态
DATASOURCES = [
    ("计量业务平台", "query_progress"),
    ("计量器具价目", "query_instrument"),
]
DOCS = [
    (CATALOG_KB, "强制检定工作计量器具实施目录(演示版).md",
     HERE / "mandatory-inspection-catalog-demo.md"),
    (REGULATION_KB, "计量器具检定规程与周期要点(演示版).md",
     HERE / "verification-regulation-demo.md"),
]


def ok(name, cond, evidence=""):
    cond = bool(cond)
    mark = "✓" if cond else "✗"
    print(f"{mark} {name}" + (f" — {evidence}" if evidence else ""))
    return cond


def find_kb(client, base, name):
    data = client.get(f"{base}/api/packs/knowledge_graph/kbs").json()
    items = data if isinstance(data, list) else (data.get("items") or [])
    for kb in items:
        if kb.get("name") == name:
            return kb
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--server-url", default="http://127.0.0.1:9771/mcp")
    args = ap.parse_args()
    client = requests.Session()
    all_ok = True

    # 1. MCP server 注册(复用 or 新建)
    servers = client.get(f"{args.base}/api/admin/mcp/servers").json()
    items = servers if isinstance(servers, list) else (servers.get("items") or [])
    server = next((s for s in items if s.get("code") == SERVER_CODE), None)
    if server is None:
        r = client.post(f"{args.base}/api/admin/mcp/servers", json={
            "name": "计量业务平台(演示)", "code": SERVER_CODE,
            "url": args.server_url, "headers": {}})
        server = r.json()
        all_ok &= ok(f"注册 MCP server {SERVER_CODE}",
                     r.status_code == 200 and server.get("status") == "connected")
    else:
        all_ok &= ok(f"MCP server {SERVER_CODE} 已注册(复用)",
                     server.get("status") == "connected")
    tools = [t.get("name") for t in (server.get("tools_snapshot") or [])]
    for need in ("query_progress", "query_instrument"):
        all_ok &= ok(f"快照含 {need}", need in tools, f"工具: {tools}")

    # 2. 知识库
    kb_ids = {}
    for name, desc in ((CATALOG_KB, "强制检定目录演示资料(南京院客服Demo)"),
                       (REGULATION_KB, "检定规程演示资料(南京院客服Demo)")):
        kb = find_kb(client, args.base, name)
        if kb is None:
            r = client.post(f"{args.base}/api/packs/knowledge_graph/kbs",
                            json={"name": name, "description": desc,
                                  "template": "general"})
            kb = r.json()
            all_ok &= ok(f"建知识库「{name}」", r.status_code == 200 and kb.get("id"))
        else:
            ok(f"知识库「{name}」已存在(复用)", True, kb["id"])
        kb_ids[name] = kb["id"]

    # 3. 上传文档(上传即导入;同内容查重跳过)
    doc_ids = []
    for kb_name, upload_name, path in DOCS:
        files = {"files": (upload_name, path.read_bytes(), "text/markdown")}
        r = client.post(
            f"{args.base}/api/packs/knowledge_graph/kbs/{kb_ids[kb_name]}/documents",
            files=files).json()
        item = (r.get("items") or [{}])[0]
        doc = item.get("document") or {}
        if item.get("ok") is False and "已存在" in str(item.get("reason") or ""):
            # 同内容查重跳过: 复用既有文档(duplicateOf 指向 doc_id)
            ok(f"文档已存在(复用): {upload_name}", True)
            doc = {"id": item.get("duplicateOf")}
        else:
            all_ok &= ok(f"上传 {upload_name}", item.get("ok") is True,
                         str(item.get("reason") or ""))
        if doc.get("id"):
            doc_ids.append((kb_name, doc["id"], upload_name))

    # 4. 等导入完成(导入走真实 LLM;必要时补触发)
    deadline = time.time() + 180
    for kb_name, doc_id, upload_name in doc_ids:
        status = ""
        while time.time() < deadline:
            docs = client.get(
                f"{args.base}/api/packs/knowledge_graph/kbs/"
                f"{kb_ids[kb_name]}/documents").json()
            doc = next((d for d in docs.get("items") or []
                        if d.get("id") == doc_id), {})
            status = doc.get("importStatus") or ""
            if status in ("succeeded", "failed"):
                break
            time.sleep(3)
        if status != "succeeded":
            # 上传即导入定型后仍 pending: 手动补触发一次再等
            client.post(f"{args.base}/api/packs/knowledge_graph/kbs/"
                        f"{kb_ids[kb_name]}/documents/{doc_id}/import", json={})
            while time.time() < deadline:
                docs = client.get(
                    f"{args.base}/api/packs/knowledge_graph/kbs/"
                    f"{kb_ids[kb_name]}/documents").json()
                doc = next((d for d in docs.get("items") or []
                            if d.get("id") == doc_id), {})
                status = doc.get("importStatus") or ""
                if status in ("succeeded", "failed"):
                    break
                time.sleep(3)
        all_ok &= ok(f"导入完成: {upload_name}", status == "succeeded", status)

    # 5. ChatBI mcp 数据源(host=server code, database=限定工具名)
    ds_ids = {}
    dss = client.get(f"{args.base}/api/packs/chatbi/datasources").json()
    for name, tool in DATASOURCES:
        ds = next((d for d in (dss.get("items") or dss) if d.get("name") == name),
                  None)
        if ds is None:
            r = client.post(f"{args.base}/api/packs/chatbi/datasources", json={
                "name": name, "db_type": "mcp", "host": SERVER_CODE, "port": 0,
                "database": tool, "username": "", "password": ""})
            ds = r.json()
            all_ok &= ok(f"建数据源「{name}」",
                         r.status_code == 200 and ds.get("id"))
        else:
            ok(f"数据源「{name}」已存在(复用)", True, ds["id"])
        ds_ids[name] = (ds or {}).get("id")

    print()
    if all_ok and ds_ids.get("计量业务平台"):
        print("Demo 环境就绪。下一步:")
        print(f"  1. .env 加 METROLOGY_PROGRESS_DATASOURCE_ID="
              f"{ds_ids['计量业务平台']}"
              " 并重启后端(或启动前 export)")
        print("  2. 前端聊天页 URL 加 ?packs=metrology_assistant")
        return 0
    print("存在失败步骤,按上方 ✗ 排查(假 server 是否在跑/后端 .env 是否配好)。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
