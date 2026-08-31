"""阶段 4B 维护目标只读接口和零副作用预览。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from aikb_web.core.maintenance_targets import (
    MAINTENANCE_TARGET_REGISTRY,
    MaintenanceTargetError,
    MaintenanceTargetStatus,
)
from aikb_web.platform.maintenance import (
    MaintenancePlan,
    MaintenancePlatformCapabilities,
    maintenance_platform_capabilities,
)

from .common import error_body, require_mutation_request, success


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class MaintenancePreviewRequest(BaseModel):
    """维护预览输入；浏览器只能提交当前服务返回的 64 位基准指纹。"""

    model_config = ConfigDict(extra="forbid")
    base_fingerprint: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _adapter(request: Request) -> Any | None:
    """取得注入的平台适配器；未接入实现时保持安全不可用。"""

    return getattr(request.app.state, "maintenance_adapter", None)


def _capabilities() -> MaintenancePlatformCapabilities:
    """只读取平台能力声明，不解析路径或创建任何维护运行材料。"""

    return maintenance_platform_capabilities()


def _unsupported_status(target_id: str) -> MaintenanceTargetStatus:
    """生成不含路径和正文的固定 unsupported 状态。"""

    target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
    return MaintenanceTargetStatus(
        target_id=target.target_id,
        status="unsupported",
        logical_leaves=target.logical_leaves,
        steps=target.steps,
        reason_code="unsupported_platform",
    )


def _read_only_available(request: Request, capabilities: MaintenancePlatformCapabilities) -> bool:
    """只读能力仅要求 Windows 和 inspect/plan 协议，不把完整写能力混入判断。"""

    adapter = _adapter(request)
    return (
        capabilities.platform == "windows"
        and callable(getattr(adapter, "inspect", None))
        and callable(getattr(adapter, "plan", None))
    )


def _platform_public(request: Request, capabilities: MaintenancePlatformCapabilities) -> dict[str, Any]:
    """公开平台能力分层；supported 保持完整维护能力，不被只读适配器改写。"""

    readonly = _read_only_available(request, capabilities)
    return {
        **capabilities.public_dict(),
        "inspection_supported": readonly,
        "preview_supported": readonly,
        "apply_supported": False,
    }


def _public_target(target_id: str) -> dict[str, Any]:
    """获取静态目标公开投影；注册表拒绝未知 ID 和路径型输入。"""

    return MAINTENANCE_TARGET_REGISTRY.get(target_id).public_dict()


def _target_not_found(request: Request) -> JSONResponse:
    """统一隐藏未知目标与路径型目标的内部校验原因。"""

    return JSONResponse(status_code=404, content=error_body("not_found", "维护目标不存在", request))


def _unavailable(request: Request) -> JSONResponse:
    """适配器缺失或内部检查失败时只返回固定不可用错误。"""

    return JSONResponse(
        status_code=503,
        content=error_body("MAINTENANCE_UNAVAILABLE", "维护服务暂不可用", request),
    )


def _state(request: Request, target_id: str) -> tuple[dict[str, Any], MaintenancePlatformCapabilities, MaintenanceTargetStatus] | JSONResponse:
    """读取一个目标的安全状态；适配器异常永不进入公开响应。"""

    try:
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
    except (MaintenanceTargetError, TypeError):
        return _target_not_found(request)
    capabilities = _capabilities()
    if not _read_only_available(request, capabilities):
        return target.public_dict(), capabilities, _unsupported_status(target.target_id)
    adapter = _adapter(request)
    if adapter is None or not callable(getattr(adapter, "inspect", None)):
        return _unavailable(request)
    try:
        inspection = adapter.inspect(target.target_id)
    except Exception:
        # 适配器底层异常可能带物理路径或系统细节，不能交给通用异常处理器。
        return _unavailable(request)
    if not isinstance(inspection, MaintenanceTargetStatus) or inspection.target_id != target.target_id:
        return _unavailable(request)
    return target.public_dict(), capabilities, inspection


@router.get("/targets")
def maintenance_targets(request: Request) -> dict[str, Any]:
    """列出三个静态维护目标和当前平台能力，不读取用户配置。"""

    capabilities = _capabilities()
    adapter = _adapter(request)
    available = capabilities.supported
    items = []
    for target in MAINTENANCE_TARGET_REGISTRY.list():
        items.append({**target.public_dict(), "supported": available})
    return success({"items": items, "platform": _platform_public(request, capabilities)}, request)


@router.get("/targets/{target_id}")
def maintenance_target_detail(request: Request, target_id: str) -> Any:
    """读取目标状态和逻辑叶子；不返回物理路径、配置正文或底层异常。"""

    state = _state(request, target_id)
    if isinstance(state, JSONResponse):
        return state
    target, capabilities, inspection = state
    # 维持 target/platform/status/leaves 四段固定包络；每段都来自静态安全模型，
    # 不把适配器返回值当作通用字典递归透传，尤其不让路径或配置正文进入响应。
    data = {
        "target": target,
        "platform": _platform_public(request, capabilities),
        "status": inspection.public_dict(),
        "leaves": [{"leaf_id": leaf_id} for leaf_id in inspection.logical_leaves],
    }
    return success(data, request)


@router.post("/targets/{target_id}/preview", dependencies=[Depends(require_mutation_request)])
def maintenance_target_preview(request: Request, target_id: str, body: MaintenancePreviewRequest) -> Any:
    """生成内存中的安全计划；不创建事务、任务、备份、临时文件或审计 probe。"""

    state = _state(request, target_id)
    if isinstance(state, JSONResponse):
        return state
    target, capabilities, inspection = state
    if inspection.status == "unsupported":
        return JSONResponse(
            status_code=409,
            content=error_body("MAINTENANCE_TARGET_UNSUPPORTED", "维护目标当前不受支持", request),
        )
    if inspection.status == "conflict":
        return JSONResponse(
            status_code=409,
            content=error_body("MAINTENANCE_CONFLICT", "维护目标存在受管冲突", request),
        )
    if inspection.status == "invalid":
        return JSONResponse(
            status_code=409,
            content=error_body("MAINTENANCE_TARGET_INVALID", "维护目标状态无效", request),
        )
    if inspection.base_fingerprint != body.base_fingerprint:
        # 指纹校验发生在 plan 前，适配器不会为陈旧请求生成任何事务材料。
        return JSONResponse(
            status_code=409,
            content=error_body("MAINTENANCE_STALE_PREVIEW", "维护目标已发生变化，请重新读取", request),
        )
    adapter = _adapter(request)
    if adapter is None or not callable(getattr(adapter, "plan", None)):
        return _unavailable(request)
    try:
        plan = adapter.plan(target["target_id"], inspection)
    except Exception:
        return _unavailable(request)
    if not isinstance(plan, MaintenancePlan) or plan.target_id != target["target_id"]:
        return _unavailable(request)
    if plan.before_fingerprint != body.base_fingerprint:
        return JSONResponse(
            status_code=409,
            content=error_body("MAINTENANCE_STALE_PREVIEW", "维护目标已发生变化，请重新读取", request),
        )
    plan_data = plan.public_dict()
    # 预览包络固定为 target/platform/inspection/plan；plan 保留模型定义的
    # ``steps`` 和 ``differences`` 结构化字段，不扩展为任意适配器对象。
    preview = {
        "target": target,
        "platform": _platform_public(request, capabilities),
        "inspection": inspection.public_dict(),
        "plan": plan_data,
    }
    return success(preview, request)


__all__ = ["MaintenancePreviewRequest", "router"]
