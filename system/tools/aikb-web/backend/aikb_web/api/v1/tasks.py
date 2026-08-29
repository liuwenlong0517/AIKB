"""阶段 3 任务 REST/SSE 接口；所有执行都委托给注入式编排服务。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Mapping

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from aikb_web.core.gateway import GatewayError
from aikb_web.core.orchestrator import TaskOrchestrator
from aikb_web.core.tasks import TERMINAL_STATES, TaskError

from .common import _sanitize_public, require_mutation_request, success, validate_task_identifier


router = APIRouter(prefix="/tasks", tags=["tasks"])
ALLOWED_SSE_TYPES = frozenset({"snapshot", "status", "progress", "output", "result", "heartbeat"})


class TaskCreateRequest(BaseModel):
    """创建任务的固定字段；额外字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")
    action_id: str
    parameters: dict[str, Any]
    preview_digest: str
    confirmation_token: str


def _orchestrator(request: Request) -> TaskOrchestrator:
    """取得应用级编排器；没有工作区或初始化失败时统一 503。"""
    service = getattr(request.app.state, "task_orchestrator", None)
    if not isinstance(service, TaskOrchestrator):
        raise GatewayError("任务服务不可用")
    return service


@router.post("", dependencies=[Depends(require_mutation_request)])
def create_task(request: Request, body: TaskCreateRequest) -> dict[str, Any]:
    """消费预览令牌并提交后台任务；不会接受命令、路径或执行器参数。"""
    task = _orchestrator(request).submit(
        action_id=body.action_id,
        parameters=body.parameters,
        preview_digest=body.preview_digest,
        confirmation_token=body.confirmation_token,
    )
    return success({"task": task}, request, allow_safe_result=True)


@router.get("")
def list_tasks(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    """列出任务安全投影；分页只在内存投影上进行，不扫描 Markdown/SQLite。"""
    items = _orchestrator(request).list_tasks()
    start = (page - 1) * page_size
    return success({"items": items[start:start + page_size], "total": len(items)}, request, allow_safe_result=True)


@router.get("/{task_id}")
def task_detail(request: Request, task_id: str) -> dict[str, Any]:
    """返回单个任务投影，非法 ID 不参与物理路径拼接。"""
    return success({"task": _orchestrator(request).get_task(validate_task_identifier(task_id))}, request, allow_safe_result=True)


@router.post("/{task_id}/cancel", dependencies=[Depends(require_mutation_request)])
def cancel_task(request: Request, task_id: str) -> dict[str, Any]:
    """请求取消并写独立审计关联；重复取消保持安全幂等。"""
    return success({"task": _orchestrator(request).cancel(validate_task_identifier(task_id))}, request, allow_safe_result=True)


def _sse_line(event: Mapping[str, Any]) -> str:
    """序列化安全 SSE 事件；正文不携带物理路径、命令或诊断字段。"""
    event_type = str(event["type"])
    public = _sanitize_public(dict(event), allow_safe_result=True)
    identifier = f"id: {int(event['event_id'])}\n" if event_type != "heartbeat" and "event_id" in event else ""
    return identifier + (
        f"event: {event_type}\n"
        f"data: {json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n\n"
    )


def _public_sse_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """把事实源的内部 output_truncated 映射为公开 output 事件。"""
    event_type = str(event.get("type") or "")
    if event_type == "output_truncated":
        return {"event_id": event.get("event_id"), "type": "output", "truncated": True}
    if event_type not in ALLOWED_SSE_TYPES - {"heartbeat"}:
        return None
    return dict(event)


async def _event_stream(service: TaskOrchestrator, task_id: str, cursor: int, initial: list[dict[str, Any]]) -> AsyncIterator[str]:
    """增量推送事件；游标失效时发送带 replay_reset 的安全 snapshot。"""
    last_heartbeat = time.monotonic()
    events = initial
    while True:
        public_events = [item for item in (_public_sse_event(raw) for raw in events) if item is not None and isinstance(item.get("event_id"), int)]
        events = public_events
        latest = events[-1]["event_id"] if events else 0
        if cursor > latest:
            snapshot = service.get_task(task_id)
            # replay_reset 使用当前事实游标，不伪造 cursor+1，客户端据此重置后续游标。
            reset = {"event_id": latest, "type": "snapshot", "replay_reset": True, "snapshot": snapshot}
            yield _sse_line(reset)
            cursor = latest
            if snapshot.get("status") in TERMINAL_STATES:
                return
        for event in events:
            event_id = event["event_id"]
            if event_id <= cursor:
                continue
            yield _sse_line(event)
            cursor = event_id
        current = service.get_task(task_id)
        if current.get("status") in TERMINAL_STATES:
            return
        await asyncio.sleep(0.25)
        if time.monotonic() - last_heartbeat >= 15:
            yield _sse_line({"type": "heartbeat", "heartbeat": True})
            last_heartbeat = time.monotonic()
        events = service.events(task_id)


@router.get("/{task_id}/events")
async def task_events(request: Request, task_id: str, last_event_id: str | None = None) -> StreamingResponse:
    """以 SSE 推送允许事件，支持 Last-Event-ID、断点 snapshot 和终态关闭。"""
    identifier = validate_task_identifier(task_id)
    service = _orchestrator(request)
    header_cursor = request.headers.get("Last-Event-ID")
    raw_cursor = header_cursor if header_cursor is not None else last_event_id
    try:
        cursor = int(raw_cursor) if raw_cursor is not None else 0
    except (TypeError, ValueError) as error:
        raise ValueError("Last-Event-ID 无效") from error
    if cursor < 0:
        raise ValueError("Last-Event-ID 无效")
    try:
        initial = service.events(identifier)
    except (TaskError, KeyError) as error:
        raise KeyError("任务不存在") from error
    return StreamingResponse(
        _event_stream(service, identifier, cursor, initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
