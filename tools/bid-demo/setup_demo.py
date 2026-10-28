#!/usr/bin/env python
"""Explicit, additive setup for the legacy metrology demonstration.

Run with backend/.venv/bin/python tools/bid-demo/setup_demo.py after configuring
model providers in the admin UI and starting the mock MCP server. This script
registers a described service, creates demo knowledge/documents and a generic MCP
datasource, then adds missing legacy demo scenarios. Existing administrator
configuration is never replaced. No model credentials are read or written here.

Legacy strong-inspection/qualification answers still depend on the knowledge
pack and explicit compatibility configuration; they are not generic MCP business
capabilities. This setup does not move those rules into the generic assistant.
"""
import argparse
from copy import deepcopy
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CATALOG_KB = "强检目录"
REGULATION_KB = "检定规程"
SERVER_CODE = "metrology"
DATASOURCE_NAME = "计量业务平台(通用演示)"
REQUIRED_TOOLS = ("query_progress", "query_instrument", "query_appointment",
                  "query_certificate", "query_report", "query_workload",
                  "query_fee_estimate")
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


def _items(data):
    return data if isinstance(data, list) else data.get("items", [])


def _get(client, url):
    result = client.get(url)
    result.raise_for_status()
    return result.json()


def ensure_demo_server(client, base, server_url):
    endpoint = f"{base}/api/admin/mcp/servers"
    servers = _items(_get(client, endpoint))
    server = next((s for s in servers if s.get("code") == SERVER_CODE), None)
    if server is None:
        result = client.post(endpoint, json={
            "name": "计量业务平台(演示)", "code": SERVER_CODE,
            "description": "演示计量业务：委托单进度、器具价目、预约、证书、报告、工作量与费用估算。",
            "url": server_url, "headers": {}, "timeout": 10, "read_timeout": 10,
        })
        result.raise_for_status()
        server = result.json()
    if (not server.get("id") or server.get("url", "").rstrip("/") != server_url.rstrip("/")
            or not server.get("enabled", True) or server.get("status") != "connected"):
        raise RuntimeError("同代码 MCP 服务 URL、启停或连接状态不符，请在管理端核对；未修改已有服务")
    tools = {t.get("name") for t in server.get("tools_snapshot", [])}
    missing = set(REQUIRED_TOOLS) - tools
    if missing:
        raise RuntimeError(f"MCP 快照缺少演示工具 {sorted(missing)}，请刷新声明后重试")
    ok("MCP 服务身份与工具已核对", True, SERVER_CODE)
    return server


def ensure_demo_datasource(client, base):
    endpoint = f"{base}/api/packs/chatbi/datasources"
    datasources = _items(_get(client, endpoint))
    ds = next((d for d in datasources if d.get("name") == DATASOURCE_NAME), None)
    if ds is None:
        result = client.post(endpoint, json={
            "name": DATASOURCE_NAME, "db_type": "mcp", "host": SERVER_CODE,
            "port": 0, "database": "", "username": "", "password": "",
        })
        result.raise_for_status()
        ds = result.json()
    if (not ds.get("id") or ds.get("dbType", ds.get("db_type")) != "mcp"
            or ds.get("host") != SERVER_CODE or ds.get("database") != ""
            or not ds.get("enabled", True)):
        raise RuntimeError("同名演示数据源 host/tool 绑定、类型或启停不符；未修改已有数据源")
    ok("通用 MCP 数据源已核对", True, ds["id"])
    return ds


def seed_demo_config(client, base, datasource_id, scenarios):
    endpoint = f"{base}/api/packs/metrology_assistant/config"
    content = deepcopy(_get(client, endpoint)["content"])
    table = content["scenarios"]
    identities = {(cfg.get("action", {}).get("server"), cfg.get("action", {}).get("tool"))
                  for cfg in table.values() if cfg.get("action", {}).get("kind") == "chatbi"}
    added = []
    for key, scenario in scenarios.items():
        cfg = deepcopy(scenario)
        action = cfg["action"]
        identity = (SERVER_CODE, action.get("tool"))
        if key in table or (action["kind"] == "chatbi" and identity in identities):
            continue
        if action["kind"] == "chatbi":
            action.update(server=SERVER_CODE, datasource_id=datasource_id)
            action.pop("env", None)
            identities.add(identity)
        table[key] = cfg
        added.append(key)
    if added:
        result = client.put(endpoint, json={
            "content": content, "comment": "显式初始化演示场景（只补缺项）",
        })
        result.raise_for_status()
    ok("演示场景补缺完成", True, ", ".join(added) if added else "已有配置保持不变")
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--server-url", default="http://127.0.0.1:9771/mcp")
    # JWT 模式服务器(配置了 ADMIN_TOKEN 的部署,如 3.110)用 secret 换
    # Bearer token;开放模式(未配置)留空直连
    ap.add_argument("--secret", default="")
    args = ap.parse_args()
    client = requests.Session()
    all_ok = True
    if args.secret:
        r = client.post(f"{args.base}/api/auth/token",
                        json={"secret": args.secret})
        r.raise_for_status()
        token = r.json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        print("✓ JWT 换取成功(鉴权模式)")
    providers = _get(client, f"{args.base}/api/admin/llm-providers")
    if not providers.get("configured"):
        print("LLM 供应商未配置：请先在管理端配置模型供应商；Demo 不读取或写入模型凭据。")
        return 2
    ok("使用管理端已有模型配置", True)
    ensure_demo_server(client, args.base, args.server_url)
    datasource = ensure_demo_datasource(client, args.base)

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

    if all_ok:
        backend_src = str(HERE.parents[1] / "backend" / "src")
        sys.path.insert(0, backend_src)
        try:
            from domains.metrology_assistant.legacy_demo import demo_scenarios
            seed_demo_config(client, args.base, datasource["id"], demo_scenarios())
        finally:
            sys.path.remove(backend_src)
        print("Demo 环境就绪，无需配置数据源环境变量或重启后端。")
        print("前端聊天页 URL 加 ?packs=metrology_assistant")
        print("限制：强检、资质及耗时仍使用显式 legacy 配置与知识库依赖，尚非独立 MCP 业务能力。")
        return 0
    print("存在失败步骤，请检查演示服务和文档导入；未补入业务场景。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
