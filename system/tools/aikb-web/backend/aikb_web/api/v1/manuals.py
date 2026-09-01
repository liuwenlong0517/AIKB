"""控制仓两份人类维护手册的固定只读接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Request

from aikb_web.core.gateway import GatewayError
from aikb_web.core.manuals import ManualNotFound

from .common import success


router = APIRouter(prefix="/manuals", tags=["manuals"])


@router.get("/{manual_id}")
def manual(
    request: Request,
    manual_id: str = Path(..., pattern=r"^(project|commands)$", max_length=32),
    max_chars: int = Query(default=500_000, ge=300, le=500_000),
) -> dict[str, Any]:
    """按固定逻辑 ID 返回项目手册或命令手册，不接受文件路径。"""
    provider = getattr(request.app.state, "manual_provider", None)
    if provider is None:
        raise GatewayError("手册服务不可用")
    try:
        result = provider.read(manual_id, max_chars=max_chars)
    except ManualNotFound:
        raise
    return success(result, request)
