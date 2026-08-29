"""API 共用的请求标识、响应和输入安全工具。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import Request


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
KNOWLEDGE_ID_PATTERN = re.compile(r"^aikb:[a-z0-9][a-z0-9:-]*$")
RUNTIME_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
AUDIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


def request_id(request: Request) -> str:
    """读取中间件生成的请求标识；未安装中间件时返回稳定的兜底值。"""
    return str(getattr(request.state, "request_id", "unknown"))


def valid_request_id(value: str | None) -> str | None:
    """只接受短 ASCII 请求标识，防止日志和响应头被注入控制字符。"""
    if value and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def validate_logical_identifier(value: str) -> str:
    """验证知识 ID 或 ``content/...`` 逻辑路径，拒绝绝对路径和目录穿越。"""
    candidate = value.strip()
    if not candidate or len(candidate) > 500:
        raise ValueError("id_or_path 无效")
    if candidate.startswith(("/", "\\")) or PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
        raise ValueError("只允许知识 ID 或相对逻辑路径")
    if re.match(r"^[A-Za-z]:", candidate) or "\x00" in candidate:
        raise ValueError("只允许知识 ID 或相对逻辑路径")
    normalized = candidate.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("逻辑路径无效")
    if candidate.startswith("aikb:") and not KNOWLEDGE_ID_PATTERN.fullmatch(candidate):
        raise ValueError("知识 ID 无效")
    if not candidate.startswith("aikb:") and not normalized.startswith("content/"):
        raise ValueError("只允许 aikb: ID 或 content/ 逻辑路径")
    return candidate


def validate_logical_prefix(value: str | None) -> str | None:
    """验证目录树筛选前缀；空值表示不筛选。"""
    if value is None or not value.strip():
        return None
    normalized = value.rstrip("/")
    if normalized == "content":
        return normalized
    return validate_logical_identifier(normalized)


def success(data: Any, request: Request, **extra_meta: Any) -> dict[str, Any]:
    """构造统一成功响应，所有响应都带请求标识和 API 版本。"""
    meta = {"request_id": request_id(request), "api_version": "v1", **extra_meta}
    return {"data": _sanitize_public(data), "meta": meta}


def _sanitize_public(value: Any) -> Any:
    """移除核心扩展对象中的物理路径字段，保留 Markdown 正文等事实内容。"""
    if isinstance(value, list):
        return [_sanitize_public(item) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "absolute_path", "filesystem_path", "database", "database_path", "traceback",
            "diagnostic", "action", "result_summary", "client", "connection_id", "payload",
            "result", "project_path", "repo_path", "repository_path", "workspace_path",
            "workspace_root", "repo_root", "knowledge_root", "content_root", "work_db", "knowledge_db",
            "source_file", "file_path", "absolute_file_path",
        }:
            continue
        if key in {"path", "source_path"} and public_logical_path(item) is None:
            continue
        sanitized[key] = _sanitize_public(item)
    return sanitized


def validate_runtime_identifier(value: str, *, name: str = "标识") -> str:
    """校验 Working State、检查点和审计关联使用的逻辑标识，不回显非法值。"""
    candidate = value.strip().lower()
    if not RUNTIME_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"{name} 无效")
    return candidate


def validate_project_id(value: str | None) -> str | None:
    """校验脱敏项目标识；项目物理路径和空段永远不进入观察接口。"""
    if value is None or not value.strip():
        return None
    candidate = value.strip().lower()
    if not RUNTIME_ID_PATTERN.fullmatch(candidate):
        raise ValueError("project_id 无效")
    return candidate


def validate_audit_identifier(value: str) -> str:
    """校验审计调用/事件标识；允许不透明技术标识但拒绝路径语法。"""
    candidate = value.strip()
    if not AUDIT_ID_PATTERN.fullmatch(candidate):
        raise ValueError("invocation_id 无效")
    return candidate


def split_csv(values: list[str] | str | None) -> list[str] | None:
    """兼容重复查询参数和逗号分隔形式，并去重保持用户顺序。"""
    if values is None:
        return None
    source = [values] if isinstance(values, str) else values
    result: list[str] = []
    for value in source:
        result.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(result)) or None


def error_body(code: str, message: str, request: Request, details: Any | None = None) -> dict[str, Any]:
    """构造不含 traceback、物理路径和内部异常文本的统一错误响应。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error, "meta": {"request_id": request_id(request), "api_version": "v1"}}


def public_logical_path(value: Any) -> str | None:
    """仅返回 content/ 开头的逻辑路径，防止核心扩展字段泄漏物理位置。"""
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    try:
        validate_logical_identifier(normalized)
    except ValueError:
        return None
    return normalized if normalized.startswith("content/") else None
