"""阶段 4A 规则目录、正文读取和候选预览接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from aikb_web.core.gateway import GatewayError
from aikb_web.core.rule_preview import RulePreviewRejected, RulePreviewService, RuleServiceError
from aikb_web.core.rule_task import RuleChangeTaskCoordinator, RuleTaskRejected

from .common import error_body, require_mutation_request, success


router = APIRouter(prefix="/rules", tags=["rules"])


class RulePreviewRequest(BaseModel):
    """浏览器预览输入；只允许当前摘要和候选正文，不接受路径或命令。"""

    model_config = ConfigDict(extra="forbid")
    base_content_hash: str = Field(..., min_length=64, max_length=64)
    candidate_content: str = Field(..., max_length=64 * 1024)


class RuleApplyRequest(BaseModel):
    """规则应用输入；浏览器只能提交服务端已生成的逻辑变更 ID 和令牌。"""

    model_config = ConfigDict(extra="forbid")
    change_id: str = Field(..., min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
    confirmation_token: str = Field(..., min_length=1, max_length=4096)


def _service(request: Request) -> RulePreviewService:
    """取得应用级规则服务；服务未初始化时统一报告不可用。"""
    service = getattr(request.app.state, "rule_preview_service", None)
    if not isinstance(service, RulePreviewService):
        raise GatewayError("规则服务不可用")
    return service


def _apply_service(request: Request) -> RuleChangeTaskCoordinator:
    """取得可注入规则应用协调器；未接入事务执行器时安全返回不可用。"""
    service = getattr(request.app.state, "rule_apply_service", None)
    if not isinstance(service, RuleChangeTaskCoordinator):
        raise RuleTaskRejected("规则应用服务不可用", status_code=404, code="not_found")
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


@router.get("/changes/{change_id}")
def rule_change_status(request: Request, change_id: str) -> Any:
    """读取规则变更与任务的安全关联状态，不公开正文、diff 或物理路径。"""
    try:
        return success(_apply_service(request).get_change(change_id), request, allow_safe_result=True)
    except RuleTaskRejected as error:
        return JSONResponse(status_code=error.status_code, content=error_body(error.code, str(error), request))


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


@router.post("/{rule_id}/apply", dependencies=[Depends(require_mutation_request)])
def rule_apply(request: Request, rule_id: str, body: dict[str, Any] | None = None) -> Any:
    """消费一次确认并创建受控规则任务；不接受正文、diff、路径或命令参数。"""
    try:
        if rule_id != "user":
            raise RuleTaskRejected("该规则只读", status_code=403, code="rule_read_only")
        # 未注入事务执行器时先返回安全 404，兼容只读部署；这样不会因缺少
        # body 在 FastAPI 参数解析阶段产生 422，误报为已开放 apply 能力。
        service = _apply_service(request)
        if body is None:
            raise RuleTaskRejected("请求参数无效", status_code=422, code="invalid_request")
        try:
            parsed = RuleApplyRequest.model_validate(body)
        except ValidationError as error:
            raise RuleTaskRejected("请求参数无效", status_code=422, code="invalid_request") from error
        data = service.apply(change_id=parsed.change_id, confirmation_token=parsed.confirmation_token)
    except RuleTaskRejected as error:
        return JSONResponse(status_code=error.status_code, content=error_body(error.code, str(error), request))
    return success(data, request)
