"""任务中心 API —— 通用后台任务的查看 / 进度 / 日志 / 取消。

【模块定位】
平台级任务观测入口(X-Admin-Token 守门,与 /api/admin 同一把口令)。
任务的业务发起口在各 pack 自己的 API(如知识图谱的文档导入端点),
这里只提供"看与停"的通用能力,零任务类型知识。

【端点清单】
  GET  /api/tasks                     → 分页列表(?status=&type=&pack=&limit=&offset=)
  GET  /api/tasks/types               → 已注册任务类型(含所属插件)
  GET  /api/tasks/{id}                → 任务详情(进度/结果/错误)
  GET  /api/tasks/{id}/logs?after=    → 日志回放(id 游标,断线补齐)
  POST /api/tasks/{id}/cancel         → 请求取消(pending 直接取消;running 协作式)
  GET  /api/tasks/{id}/events         → SSE 实时事件流(snapshot/progress/log/status)
"""
import asyncio
import logging
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.admin import _int_param, require_admin
from api.sse import SSEEvent
from services.task_store import FINAL_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(require_admin)])

# SSE 响应头:与 api/config.py 的对话流同一套(防代理攒包/缓存)
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


@router.get("")
async def list_tasks(request: Request):
    """任务分页列表(任务中心表格;按需轮询)。"""
    manager = request.app.state.task_manager
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    status = (request.query_params.get("status") or "").strip() or None
    task_type = (request.query_params.get("type") or "").strip() or None
    pack_name = (request.query_params.get("pack") or "").strip() or None
    items, total = manager.store.list_tasks(
        status=status, task_type=task_type, pack_name=pack_name,
        limit=limit, offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/types")
async def list_task_types(request: Request):
    """已注册任务类型清单(注意声明在 /{task_id} 之前,防路径误匹配)。"""
    return {"items": request.app.state.task_manager.registered_types()}


def _get_task_or_404(request: Request, task_id: str) -> Dict[str, Any]:
    task = request.app.state.task_manager.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request):
    """任务详情(进度条/结果/错误——任务抽屉的数据源)。"""
    return _get_task_or_404(request, task_id)


@router.get("/{task_id}/logs")
async def get_task_logs(task_id: str, request: Request):
    """日志回放:id 游标分页(after=上次读到的最大 id)。"""
    _get_task_or_404(request, task_id)
    after = max(0, _int_param(request, "after", 0))
    limit = max(1, min(_int_param(request, "limit", 500), 2000))
    logs = request.app.state.task_manager.store.list_logs(task_id, after_id=after, limit=limit)
    return {"items": logs, "lastId": logs[-1]["id"] if logs else after}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """请求取消(幂等;已终态的任务原样返回)。"""
    manager = request.app.state.task_manager
    task = manager.cancel(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/{task_id}/events")
async def task_events(task_id: str, request: Request):
    """任务实时事件流(SSE)。

    事件序列:先 snapshot(当前任务 + 历史日志),随后 progress/log/status
    增量;status 进入终态后收流。日志按 id 与快照去重(订阅与拍快照之间
    的小窗口可能重复投递)。
    """
    manager = request.app.state.task_manager
    # 存在性预检在外(404 判定),订阅在生成器体内——StreamingResponse 在
    # 客户端立即断开时可能从不迭代生成器,在外面 subscribe 会留下永不
    # 清理的监听队列(每个事件都触发"队列满丢帧"告警直到上限)。
    task = manager.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    async def event_stream():
        snapshot_task, snapshot_logs, q = manager.subscribe(task_id)
        max_log_id = max((lg["id"] for lg in snapshot_logs), default=0)
        loop = asyncio.get_running_loop()
        aq: asyncio.Queue = asyncio.Queue()
        closed = threading.Event()

        def pump():
            """桥线程:线程安全 queue.Queue → 事件循环的 asyncio.Queue。

            与 StreamManager 的 call_soon_threadsafe 桥接同一模式;
            0.5s 轮询 closed 事件保证连接断开时线程能退出。
            """
            while not closed.is_set():
                try:
                    item = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                loop.call_soon_threadsafe(aq.put_nowait, item)

        bridge = threading.Thread(target=pump, daemon=True, name=f"task-sse-{task_id[:8]}")
        bridge.start()
        try:
            yield SSEEvent("snapshot", {"task": snapshot_task, "logs": snapshot_logs}).to_sse()
            if snapshot_task["status"] in FINAL_STATUSES:
                return
            while True:
                try:
                    item = await asyncio.wait_for(aq.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE 注释行,保活防代理断连
                    continue
                event = item.get("event")
                data = item.get("data") or {}
                if event == "log" and data.get("id") and data["id"] <= max_log_id:
                    continue  # 与快照重复的日志,跳过
                yield SSEEvent(event, data).to_sse()
                if event == "status" and data.get("status") in FINAL_STATUSES:
                    break
        finally:
            closed.set()
            manager.unsubscribe(task_id, q)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS
    )
