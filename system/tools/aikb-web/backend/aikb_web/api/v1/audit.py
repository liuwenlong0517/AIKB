"""阶段 2 审计只读观察接口。"""

from __future__ import annotations

import re

from typing import Any

from fastapi import APIRouter, Query, Request

from aikb_web.core.gateway import GatewayError

from .common import split_csv, success, validate_audit_identifier, validate_project_id


router = APIRouter(prefix="/audit", tags=["audit"])

# change_id 由规则事务层生成，格式比历史 invocation/event 标识更窄，
# 因此不能复用允许冒号的通用审计标识校验器。
CHANGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _validate_change_id(value: str) -> str:
    """校验规则变更标识，不接受路径语法、冒号或控制字符。"""
    candidate = value.strip()
    if not CHANGE_ID_PATTERN.fullmatch(candidate):
        raise ValueError("change_id 无效")
    return candidate


AUDIT_STATUSES = {
    "started", "succeeded", "failed", "noop", "blocked", "incomplete",
    "cancelled", "timed_out", "interrupted",
}
AUDIT_SOURCES = {"mcp", "hook", "web"}
AUDIT_RESOURCE_TYPES = {"rule"}
AUDIT_RESOURCE_IDS = {"entry", "user", "agent", "contributing"}


def _gateway(request: Request, capability: str = "web_audit_query") -> Any:
    """取得实现共享审计安全读模型的网关。"""
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    if gateway is None or not callable(getattr(gateway, capability, None)):
        raise GatewayError("审计服务不可用")
    return gateway


def _filters(
    *, since: str | None, on_date: str | None, agent: str | None, source: str | None,
    status: list[str] | None, operation: str | None, session_label: str | None,
    project_id: str | None, change_id: str | None, resource_type: str | None, resource_id: str | None,
) -> dict[str, Any]:
    """校验并规范化审计筛选项，所有错误均不回显用户输入。"""
    if since and on_date:
        raise ValueError("since 与 date 不能同时使用")
    statuses = split_csv(status)
    if statuses and any(item not in AUDIT_STATUSES for item in statuses):
        raise ValueError("status 无效")
    normalized_source = source.strip().lower() if source else None
    if normalized_source and normalized_source not in AUDIT_SOURCES:
        raise ValueError("source 无效")
    normalized_change_id = _validate_change_id(change_id) if change_id else None
    normalized_resource_type = resource_type.strip().lower() if resource_type else None
    if normalized_resource_type and normalized_resource_type not in AUDIT_RESOURCE_TYPES:
        raise ValueError("resource_type 无效")
    normalized_resource_id = resource_id.strip().lower() if resource_id else None
    if normalized_resource_id and normalized_resource_id not in AUDIT_RESOURCE_IDS:
        raise ValueError("resource_id 无效")
    if normalized_resource_id and not normalized_resource_type:
        raise ValueError("resource_type 无效")
    return {
        "since": since.strip() if since else None,
        "on_date": on_date.strip() if on_date else None,
        "agent": agent.strip() if agent else None,
        "source": normalized_source,
        # 共享读模型原生支持单值和多值状态，保留列表可避免多次扫描审计事实源。
        "status": statuses[0] if statuses and len(statuses) == 1 else statuses,
        "operation": operation.strip() if operation else None,
        "session_label": session_label.strip() if session_label else None,
        "project_id": validate_project_id(project_id),
        "change_id": normalized_change_id,
        "resource_type": normalized_resource_type,
        "resource_id": normalized_resource_id,
    }


def _audit_meta(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """把损坏审计记录映射成机器可读警告，绝不返回文件名或行号。"""
    summary = data.get("summary") if isinstance(data, dict) else data
    damaged = int(summary.get("damaged_count") or 0) if isinstance(summary, dict) else 0
    return bool(damaged), (["damaged_records", "audit_partial"] if damaged else [])


@router.get("/summary")
def summary(
    request: Request,
    since: str | None = Query(default=None, max_length=16),
    on_date: str | None = Query(default=None, alias="date", max_length=10),
    agent: str | None = Query(default=None, max_length=120),
    source: str | None = Query(default=None, max_length=16),
    status: list[str] | None = Query(default=None),
    operation: str | None = Query(default=None, max_length=120),
    session_label: str | None = Query(default=None, max_length=240),
    project_id: str | None = Query(default=None, max_length=120),
    change_id: str | None = Query(default=None, max_length=120),
    resource_type: str | None = Query(default=None, max_length=16),
    resource_id: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """返回脱敏审计汇总；空记录是合法 200 空汇总。"""
    filters = _filters(
        since=since, on_date=on_date, agent=agent, source=source, status=status,
        operation=operation, session_label=session_label, project_id=project_id, change_id=change_id,
        resource_type=resource_type, resource_id=resource_id,
    )
    data = _gateway(request, "web_audit_summary").web_audit_summary(**filters)
    degraded, warnings = _audit_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/events")
def events(
    request: Request,
    since: str | None = Query(default=None, max_length=16),
    on_date: str | None = Query(default=None, alias="date", max_length=10),
    agent: str | None = Query(default=None, max_length=120),
    source: str | None = Query(default=None, max_length=16),
    status: list[str] | None = Query(default=None),
    operation: str | None = Query(default=None, max_length=120),
    session_label: str | None = Query(default=None, max_length=240),
    project_id: str | None = Query(default=None, max_length=120),
    change_id: str | None = Query(default=None, max_length=120),
    resource_type: str | None = Query(default=None, max_length=16),
    resource_id: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """返回按最新活动倒序的安全审计调用列表和分页信息。"""
    filters = _filters(
        since=since, on_date=on_date, agent=agent, source=source, status=status,
        operation=operation, session_label=session_label, project_id=project_id, change_id=change_id,
        resource_type=resource_type, resource_id=resource_id,
    )
    data = _gateway(request, "web_audit_query").web_audit_query(**filters, page=page, page_size=page_size)
    degraded, warnings = _audit_meta(data)
    return success(data, request, degraded=degraded, warnings=warnings)


@router.get("/events/{invocation_id}")
def event_detail(request: Request, invocation_id: str) -> dict[str, Any]:
    """按 invocation/event 标识读取一次调用的有限安全详情。"""
    # 详情函数在共享核心中再次进行有界标识匹配；这里不回显非法值。
    identifier = validate_audit_identifier(invocation_id)
    data = _gateway(request, "web_audit_detail").web_audit_detail(identifier)
    if data is None:
        raise KeyError("审计调用不存在")
    return success(data, request, degraded=False, warnings=[])
