"""
Mock API Server — 万能测试接口。
任何请求都返回成功，只打日志。

用法:
    python3 mock_api_server.py [--port 19999]

所有 POST/GET 请求:
    - 打印 method + path + body 到 stdout
    - 返回 {"success": true, "mock": true, ...}
"""
import argparse
import json
import logging
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MOCK] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock API Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    """万能接口 — 记录所有请求，返回成功。"""
    method = request.method
    body = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = await request.body()
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")

    # 打日志
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "method": method,
        "path": f"/{path}",
        "query": dict(request.query_params),
        "body": body,
    }
    logger.info(f">>> {method} /{path}")
    if body:
        body_str = json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, dict) else str(body)
        for line in body_str.split("\n")[:10]:
            logger.info(f"    {line}")
        if body_str.count("\n") > 10:
            logger.info(f"    ... ({body_str.count(chr(10))} lines total)")

    # 根据路径返回合理的 mock 数据
    # 统一返回 "success" 字段(与 AssetClient 归一化约定一致)
    if "validate" in path:
        return {"success": True, "pass": True, "errors": [], "warnings": ["[mock] 模拟校验通过"]}
    elif "submit" in path or "create" in path or "apply" in path:
        return {
            "success": True,
            "mock": True,
            "id": f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "message": "[mock] 提交成功",
        }
    elif "approve" in path:
        return {
            "success": True,
            "status": "approved",
            "mock": True,
            "message": "[mock] 审批通过",
        }
    elif "guide" in path:
        return {
            "mock": True,
            "fieldTypes": [
                {"type": 0, "name": "文本"},
                {"type": 2, "name": "日期"},
                {"type": 4, "name": "下拉选择"},
                {"type": 7, "name": "人员选择"},
            ],
        }
    else:
        return {
            "success": True,
            "mock": True,
            "message": f"[mock] {method} /{path} OK",
            "echo": body,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19999)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    logger.info(f"Mock API Server starting on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
