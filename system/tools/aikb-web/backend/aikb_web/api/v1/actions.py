"""阶段 3 动作目录和预览接口；预览本身不执行任何动作。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from aikb_web.core.actions import ActionError, ActionRegistry, ConfirmationTokenService
from aikb_web.core.gateway import GatewayError
from aikb_web.platform import platform_state

from .common import require_mutation_request, success


router = APIRouter(prefix="/actions", tags=["actions"])


class PreviewRequest(BaseModel):
    """预览请求只允许规范化参数字段。"""

    model_config = ConfigDict(extra="forbid")
    parameters: dict[str, Any] = Field(default_factory=dict)


def _registry(request: Request) -> ActionRegistry:
    """取得静态动作注册表；缺失时按服务不可用处理。"""
    registry = getattr(request.app.state, "action_registry", None)
    if not isinstance(registry, ActionRegistry):
        raise GatewayError("动作服务不可用")
    return registry


def _tokens(request: Request) -> ConfirmationTokenService:
    """取得进程内确认令牌服务，不在请求中接受客户端密钥。"""
    service = getattr(request.app.state, "confirmation_tokens", None)
    if not isinstance(service, ConfirmationTokenService):
        raise GatewayError("确认服务不可用")
    return service


@router.get("")
def actions(request: Request) -> dict[str, Any]:
    """列出静态动作能力及其风险/并发元数据。"""
    current = platform_state().platform
    runtime_available = getattr(request.app.state, "platform_action_available", False)
    items = []
    for item in _registry(request).list():
        supported = current in item.get("supported_platforms", []) and (current != "windows" or runtime_available)
        reason = None if supported else ("executor_unavailable" if current == "windows" else "platform_not_supported")
        items.append({**item, "supported": supported, "reason": reason})
    return success({"items": items}, request)


@router.post("/{action_id}/preview", dependencies=[Depends(require_mutation_request)])
def preview(request: Request, action_id: str, body: PreviewRequest) -> dict[str, Any]:
    """规范化参数并签发五分钟单次令牌；read_only 也必须经过此流程。"""
    # 真实服务未完成平台适配或运行在不支持平台时，预览也不能成为绕过能力
    # 检查的入口；测试必须显式注入对应平台状态和可用执行器。
    try:
        registry = _registry(request)
        spec = registry.get(action_id)
        current = platform_state().platform
        if current not in spec.supported_platforms:
            raise ValueError("动作在当前平台不可用")
        if getattr(request.app.state, "task_orchestrator", None) is None or not getattr(request.app.state, "platform_action_available", False):
            raise ValueError("动作执行器不可用")
        data = registry.preview(action_id, body.parameters)
    except ActionError as error:
        raise ValueError("动作或参数无效") from error
    token_service = _tokens(request)
    token = token_service.issue(
        action_id=action_id,
        parameters=data["parameters"],
        risk_level=data["risk_level"],
        preview_digest=data["preview_digest"],
    )
    return success(
        {"preview": data, "confirmation_token": token, "expires_in_seconds": token_service.TTL_SECONDS},
        request,
    )
