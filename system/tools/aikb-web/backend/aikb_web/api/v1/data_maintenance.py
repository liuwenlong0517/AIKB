"""Workspace 数据维护的固定类别清单、预览与受控应用接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from aikb_web.core.workspace_cleanup import WorkspaceCleanupError, WorkspaceCleanupService

from .common import error_body, require_mutation_request, success


router = APIRouter(prefix="/data-maintenance", tags=["data-maintenance"])


class CleanupPreviewRequest(BaseModel):
    """浏览器只能选择固定类别和有界保留天数，不能提交路径。"""

    model_config = ConfigDict(extra="forbid")
    categories: list[str] = Field(..., min_length=1, max_length=3)
    retention_days: dict[str, int] = Field(default_factory=dict)


class CleanupApplyRequest(BaseModel):
    """应用请求只携带服务端计划 ID 对应的一次性确认令牌。"""

    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(..., min_length=1, max_length=4096)


def _service(request: Request) -> WorkspaceCleanupService:
    service = getattr(request.app.state, "data_maintenance_service", None)
    if not isinstance(service, WorkspaceCleanupService):
        raise WorkspaceCleanupError("数据维护服务暂不可用")
    return service


def _error(request: Request, error: WorkspaceCleanupError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error_body(error.code, str(error), request, error.details or None))


@router.get("")
def overview(request: Request) -> Any:
    """读取默认策略下的安全盘点，不执行删除或创建清理材料。"""

    try:
        return success(_service(request).overview(), request)
    except WorkspaceCleanupError as error:
        return _error(request, error)


@router.post("/preview", dependencies=[Depends(require_mutation_request)])
def preview(request: Request, body: CleanupPreviewRequest) -> Any:
    """为固定类别生成短期计划；正文和物理路径均不进入协议。"""

    try:
        return success(_service(request).preview(categories=body.categories, retention_days=body.retention_days), request)
    except WorkspaceCleanupError as error:
        return _error(request, error)


@router.post("/plans/{plan_id}/apply", dependencies=[Depends(require_mutation_request)])
def apply(request: Request, plan_id: str, body: CleanupApplyRequest) -> Any:
    """重新扫描并消费一次确认，陈旧预览和并发维护均拒绝执行。"""

    try:
        return success(_service(request).apply(plan_id, body.confirmation_token), request, allow_safe_result=True)
    except WorkspaceCleanupError as error:
        return _error(request, error)


__all__ = ["CleanupApplyRequest", "CleanupPreviewRequest", "router"]
