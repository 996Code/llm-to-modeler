#!/usr/bin/env python
"""南京院客服 Demo 环境一键准备(幂等: 已存在的注册/库/文档/数据源复用)。

前置:
  1. 计量假 server 在跑:  cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py
  2. 后端 dev server 在跑(默认 http://localhost:18080, .env 已配 LLM/DATABASE_URL)

做七件事:
  0. 确保 LLM 供应商已配置(供应商页已配则复用;未配且 env 有
     LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 时经管理 API 种入 DB——
     一次性引导,运行时只认 DB;两边都没有则报错退出)
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
    # JWT 模式服务器(配置了 ADMIN_TOKEN 的部署,如 3.110)用 secret 换
    # Bearer token;开放模式(未配置)留空直连
    ap.add_argument("--secret", default="")
    args = ap.parse_args()
    client = requests.Session()
    if args.secret:
        r = client.post(f"{args.base}/api/auth/token",
                        json={"secret": args.secret})
        r.raise_for_status()
        token = r.json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        print("✓ JWT 换取成功(鉴权模式)")
    # 0. LLM 供应商(DB 唯一事实源):已配置复用;未配置且 env 可引导则
    #    经管理 API 种入(导入/对话全走 DB 配置,后续改配置在管理页);
    #    两边都没有 → 明确报错退出,不让 LLM 抽取在半路晦涩失败
    import os
    providers = client.get(f"{args.base}/api/admin/llm-providers").json()
    if providers.get("configured"):
        all_ok &= ok("LLM 供应商已配置(复用)",
                     bool((providers.get("primary") or {}).get("base_url")),
                     f"primary → {(providers.get('primary') or {}).get('base_url')}")
    else:
        env_base = os.getenv("LLM_BASE_URL", "").strip()
        env_key = os.getenv("LLM_API_KEY", "").strip()
        env_model = os.getenv("LLM_MODEL", "").strip()
        if not env_base or not env_model:
            print("✗ LLM 供应商未配置:请在管理端「插件管理 → 模型供应商」"
                  "维护主端点(base_url/api_key/model),或设置 "
                  "LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 环境变量后重跑本脚本"
                  "(将自动经 API 种入)")
            return 2
        r = client.put(f"{args.base}/api/admin/llm-providers", json={
            "primary": {"base_url": env_base, "api_key": env_key,
                        "model": env_model, "enabled": True},
            "expected_revision": providers.get("revision", 0),
        })
        all_ok &= ok("LLM 供应商经 API 种入 DB(env 引导,一次性)",
                     r.status_code == 200,
                     f"{env_base} · {env_model}" if r.status_code == 200
                     else r.text[:200])
        if r.status_code != 200:
            return 2
    all_ok = True

    # 1. MCP server 注册(复用 or 新建;超时演示依赖 timeout/read_timeout=10s,
    #    注册默认 30/300 会让 BJ0000 拖到 32s+——干净机器必配)
    servers = client.get(f"{args.base}/api/admin/mcp/servers").json()
    items = servers if isinstance(servers, list) else (servers.get("items") or [])
    server = next((s for s in items if s.get("code") == SERVER_CODE), None)
    if server is None:
        r = client.post(f"{args.base}/api/admin/mcp/servers", json={
            "name": "计量业务平台(演示)", "code": SERVER_CODE,
            "url": args.server_url, "headers": {},
            "timeout": 10, "read_timeout": 10})
        server = r.json()
        all_ok &= ok(f"注册 MCP server {SERVER_CODE}",
                     r.status_code == 200 and server.get("status") == "connected")
    else:
        all_ok &= ok(f"MCP server {SERVER_CODE} 已注册(复用)",
                     server.get("status") == "connected")
    if server and (float(server.get("timeout") or 0) > 10
                   or float(server.get("read_timeout") or 0) > 10):
        server = client.put(
            f"{args.base}/api/admin/mcp/servers/{server['id']}",
            json={"timeout": 10, "read_timeout": 10}).json()
        all_ok &= ok("timeout/read_timeout 对齐 10s(超时演示依赖)",
                     float(server.get("timeout") or 0) <= 10
                     and float(server.get("read_timeout") or 0) <= 10,
                     f"timeout={server.get('timeout')}s "
                     f"read_timeout={server.get('read_timeout')}s")
    else:
        ok("timeout/read_timeout 已是 10s 级", True,
           f"timeout={server.get('timeout')}s "
           f"read_timeout={server.get('read_timeout')}s")
    tools = [t.get("name") for t in (server.get("tools_snapshot") or [])]
    for need in ("query_progress", "query_instrument",
                 "query_appointment", "query_certificate", "query_report"):
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
            if r.status_code != 200 and "已存在" in str(kb.get("detail") or ""):
                # 列表读在服务重启/热装配窗口短暂空,POST 真实冲突;
                # 重新列表拿权威 id,幂等脚本不因瞬时读视图失真而中断
                kb = find_kb(client, args.base, name) or {}
                all_ok &= ok(f"知识库「{name}」已存在(并发复用)",
                             bool(kb.get("id")), str(kb.get("id") or ""))
            else:
                all_ok &= ok(f"建知识库「{name}」", r.status_code == 200 and kb.get("id"))
        if not kb.get("id"):
            raise RuntimeError(f"知识库「{name}」创建/查找失败: {kb}")
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
        # 空列表是 falsy,不能 `or dss`(会迭代 dict 的字符串键炸 .get)
        ds = next((d for d in (dss.get("items") or []) if d.get("name") == name),
                  None)
        if ds is None:
            r = client.post(f"{args.base}/api/packs/chatbi/datasources", json={
                "name": name, "db_type": "mcp", "host": SERVER_CODE, "port": 0,
                "database": tool, "username": "", "password": ""})
            ds = r.json()
            if r.status_code != 200 and "已存在" in str(ds.get("detail") or ""):
                dss2 = client.get(f"{args.base}/api/packs/chatbi/datasources").json()
                ds = next((d for d in (dss2.get("items") or [])
                           if d.get("name") == name), {})
                all_ok &= ok(f"数据源「{name}」已存在(并发复用)",
                             bool(ds.get("id")), str(ds.get("id") or ""))
            else:
                all_ok &= ok(f"建数据源「{name}」",
                             r.status_code == 200 and ds.get("id"))
        else:
            ok(f"数据源「{name}」已存在(复用)", True, ds["id"])
        if not ds.get("id"):
            raise RuntimeError(f"数据源「{name}」创建/查找失败: {ds}")
        ds_ids[name] = ds["id"]

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
