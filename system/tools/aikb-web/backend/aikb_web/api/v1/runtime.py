"""阶段 2 Working State 只读观察接口。

路由只校验 HTTP 参数并调用网关；Working State Markdown 和派生索引的读取、
脱敏及长度限制由共享 ``WorkStateStore.web_*`` 契约负责。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from aikb_web.core.gateway import GatewayError

from .common import split_csv, success, validate_project_id, validate_runtime_identifier


router = APIRouter(prefix="/runtime", tags=["runtime"])


def _gateway(request: Request, capability: str = "web_active_work_states") -> Any:
    """取得应用级共享网关；缺失观察能力时由统一异常边界返回 503。"""
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    if gateway is None or not callable(getattr(gateway, capability, None)):
        raise GatewayError("运行状态服务不可用")
    return gateway


def _runtime_meta(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """根据共享读模型的状态生成降级元数据，不把内部原因带到响应。"""
    index = data.get("index") if isinstance(data, dict) else None
    if not isinstance(index, dict):
        return False, []
    status = str(index.get("status") or "ready")
    if status == "unavailable":
        # 无法保证分页完整性时不能伪造空列表。
        raise GatewayError("运行状态索引不可用")
    return status != "ready", (["index_rebuilt"] if status == "rebuilt" else [])


@router.get("/working-states")
def working_states(
    request: Request,
    project_id: str | None = Query(default=None, max_length=120),
    status: list[str] | None = Query(default=None),
    agent: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """列出 planned、active、blocked 活动任务；不开放归档搜索和任何写入。"""
    gateway = _gateway(request)
    data = gateway.web_active_work_states(
        project_id=validate_project_id(project_id),
        status=split_csv(status),
        agent=agent.strip() if agent else None,
        page=page,
        page_size=page_size,
    )
    degraded, warnings = _runtime_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/archived-working-states")
def archived_working_states(
    request: Request,
    project_id: str | None = Query(default=None, max_length=120),
    status: list[str] | None = Query(default=None),
    agent: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """列出 completed、abandoned、superseded 历史任务；与活动接口完全分离。"""
    gateway = _gateway(request, "web_archived_work_states")
    data = gateway.web_archived_work_states(
        project_id=validate_project_id(project_id),
        status=split_csv(status),
        agent=agent.strip() if agent else None,
        page=page,
        page_size=page_size,
    )
    degraded, warnings = _runtime_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/working-states/{work_id}")
def working_state(request: Request, work_id: str) -> dict[str, Any]:
    """读取一个活动任务的有限详情，所有正文和仓库字段由共享核心裁剪。"""
    identifier = validate_runtime_identifier(work_id, name="work_id")
    data = _gateway(request, "web_work_state").web_work_state(identifier)
    degraded, warnings = _runtime_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/archived-working-states/{work_id}")
def archived_working_state(request: Request, work_id: str) -> dict[str, Any]:
    """读取历史任务的安全详情；归档内容只读且不暴露物理路径。"""
    identifier = validate_runtime_identifier(work_id, name="work_id")
    data = _gateway(request, "web_archived_work_state").web_archived_work_state(identifier)
    degraded, warnings = _runtime_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/working-states/{work_id}/checkpoints")
def checkpoints(
    request: Request,
    work_id: str,
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """列出活动任务检查点摘要；查询不会创建、修改或关闭任务。"""
    identifier = validate_runtime_identifier(work_id, name="work_id")
    data = _gateway(request, "web_checkpoints").web_checkpoints(identifier, page=page, page_size=page_size)
    return success(data, request, degraded=False, warnings=[])


@router.get("/archived-working-states/{work_id}/checkpoints")
def archived_checkpoints(
    request: Request,
    work_id: str,
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """分页读取历史任务检查点；不会创建、修改或重新打开任务。"""
    identifier = validate_runtime_identifier(work_id, name="work_id")
    data = _gateway(request, "web_archived_checkpoints").web_archived_checkpoints(identifier, page=page, page_size=page_size)
    return success(data, request, degraded=False, warnings=[])


@router.get("/working-states/{work_id}/checkpoints/{checkpoint_id}")
def checkpoint(request: Request, work_id: str, checkpoint_id: str) -> dict[str, Any]:
    """读取白名单检查点章节；不返回源文件路径、完整 Markdown 或原始输入。"""
    work_identifier = validate_runtime_identifier(work_id, name="work_id")
    checkpoint_identifier = validate_runtime_identifier(checkpoint_id, name="checkpoint_id")
    data = _gateway(request, "web_checkpoint").web_checkpoint(work_identifier, checkpoint_identifier)
    return success(data, request, degraded=False, warnings=[])


@router.get("/archived-working-states/{work_id}/checkpoints/{checkpoint_id}")
def archived_checkpoint(request: Request, work_id: str, checkpoint_id: str) -> dict[str, Any]:
    """读取历史检查点的安全字段；不返回源文件路径或原始 Markdown。"""
    work_identifier = validate_runtime_identifier(work_id, name="work_id")
    checkpoint_identifier = validate_runtime_identifier(checkpoint_id, name="checkpoint_id")
    data = _gateway(request, "web_archived_checkpoint").web_archived_checkpoint(work_identifier, checkpoint_identifier)
    return success(data, request, degraded=False, warnings=[])
