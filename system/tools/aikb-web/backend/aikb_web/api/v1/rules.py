"""阶段 4A 规则目录、正文读取和候选预览接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from aikb_web.core.gateway import GatewayError
from aikb_web.core.rule_preview import RulePreviewRejected, RulePreviewService, RuleServiceError

from .common import error_body, require_mutation_request, success


router = APIRouter(prefix="/rules", tags=["rules"])


class RulePreviewRequest(BaseModel):
    """浏览器预览输入；只允许当前摘要和候选正文，不接受路径或命令。"""

    model_config = ConfigDict(extra="forbid")
    base_content_hash: str = Field(..., min_length=64, max_length=64)
    candidate_content: str = Field(..., max_length=64 * 1024)


def _service(request: Request) -> RulePreviewService:
    """取得应用级规则服务；服务未初始化时统一报告不可用。"""
    service = getattr(request.app.state, "rule_preview_service", None)
    if not isinstance(service, RulePreviewService):
        raise GatewayError("规则服务不可用")
    return service


@router.get("")
def rules(request: Request) -> dict[str, Any]:
    """列出固定四项规则及可读/可写能力和当前安全摘要。"""
    try:
        return success({"items": _service(request).list_rules()}, request)
    except RuleServiceError as error:
        # 服务内部异常可能携带文件异常链；交给主入口的安全 GatewayError
        # 处理器，避免通用 LOGGER.exception 记录底层路径或 traceback。
        raise GatewayError("规则服务暂不可用") from error


@router.get("/{rule_id}")
def rule_detail(request: Request, rule_id: str) -> Any:
    """读取规则正文；规则 ID 由服务端静态注册表解释，不接受路径。"""
    try:
        return success(_service(request).get_rule(rule_id), request)
    except RulePreviewRejected as error:
        return JSONResponse(
            status_code=error.status_code,
            content=error_body(error.code, str(error), request, error.details),
        )
    except RuleServiceError as error:
        # 与目录接口保持相同的安全异常边界，不把内部错误交给通用异常日志。
        raise GatewayError("规则服务暂不可用") from error


@router.post("/{rule_id}/preview", dependencies=[Depends(require_mutation_request)])
def rule_preview(request: Request, rule_id: str, body: RulePreviewRequest) -> Any:
    """仅为 user 规则生成完整 diff 和短期令牌，不执行写入或创建任务。"""
    try:
        data = _service(request).preview(
            rule_id,
            base_content_hash=body.base_content_hash,
            candidate_content=body.candidate_content,
        )
    except RulePreviewRejected as error:
        # 校验错误只投影共享验证器的字段级安全摘要，永不回显候选正文或物理路径。
        return JSONResponse(
            status_code=error.status_code,
            content=error_body(error.code, str(error), request, error.details),
        )
    except RuleServiceError as error:
        # 预览材料失败时只返回安全的 503；内部异常不进入通用 exception 日志。
        raise GatewayError("规则服务暂不可用") from error
    return success(data, request)
