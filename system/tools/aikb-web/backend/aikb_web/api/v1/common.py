"""API 共用的请求标识、响应和输入安全工具。"""

from __future__ import annotations

import re
import os
from urllib.parse import urlsplit
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import Request


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
KNOWLEDGE_ID_PATTERN = re.compile(r"^aikb:[a-z0-9][a-z0-9:-]*$")
RUNTIME_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
TASK_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
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


def success(data: Any, request: Request, *, allow_safe_result: bool = False, **extra_meta: Any) -> dict[str, Any]:
    """构造统一成功响应，所有响应都带请求标识和 API 版本。"""
    meta = {"request_id": request_id(request), "api_version": "v1", **extra_meta}
    return {"data": _sanitize_public(data, allow_safe_result=allow_safe_result), "meta": meta}


_TASK_RESULT_DENY_KEYS = {
    "absolute_path", "filesystem_path", "project_path", "repo_path", "repository_path", "workspace_path",
    "workspace_root", "repo_root", "knowledge_root", "content_root", "source_file", "file_path",
    "absolute_file_path", "path", "source_path", "command", "commands", "cmd", "argv", "token",
    "confirmation_token", "secret", "password", "passwd", "authorization", "cookie", "diagnostic",
    "traceback", "payload", "raw", "stdout", "stderr",
}

_PUBLIC_DENY_KEYS = {
    "absolute_path", "filesystem_path", "database", "database_path", "traceback",
    "diagnostic", "action", "result_summary", "client", "connection_id", "payload",
    "result", "project_path", "repo_path", "repository_path", "workspace_path",
    "workspace_root", "repo_root", "knowledge_root", "content_root", "work_db", "knowledge_db",
    "source_file", "file_path", "absolute_file_path",
}
_PUBLIC_PATH_KEY_SUFFIXES = ("_path", "_root", "_directory", "_dir", "_file")
_PUBLIC_TEXT_KEYS = frozenset({"content"})
_LOCAL_PATH_PLACEHOLDER = "[LOCAL_PATH]"


def _sanitize_safe_result(value: Any) -> Any:
    """递归投影 TaskStore 已脱敏结果；任务专用白名单不得扩散到其他 API。"""
    if isinstance(value, list):
        return [_sanitize_safe_result(item) for item in value[:50]]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:50]:
        normalized = str(key).strip().lower()
        if normalized in _TASK_RESULT_DENY_KEYS or normalized.endswith("_path") or normalized.endswith("_token"):
            continue
        result[str(key)] = _sanitize_safe_result(item)
    return result


def _is_absolute_local_path(value: Any) -> bool:
    """识别完整本地绝对路径值；逻辑路径与 HTTP(S) URL 不属于本地文件位置。"""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or public_logical_path(candidate) is not None:
        return False
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() in {"http", "https"}:
        return False
    if parsed.scheme.lower() == "file":
        return True
    return (
        PureWindowsPath(candidate).is_absolute()
        or candidate.startswith(("\\\\", "\\"))
        or PurePosixPath(candidate).is_absolute()
    )


def _sanitize_public(value: Any, *, allow_safe_result: bool = False, _field_name: str | None = None) -> Any:
    """递归移除物理路径字段和值，同时保留明确允许的 Markdown 正文与逻辑路径。"""
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_public(item, allow_safe_result=allow_safe_result, _field_name=_field_name)
            for item in value
        ]
    if not isinstance(value, dict):
        if _field_name not in _PUBLIC_TEXT_KEYS and _is_absolute_local_path(value):
            return _LOCAL_PATH_PLACEHOLDER
        return value
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        public_key = str(key)
        normalized = re.sub(r"[^a-z0-9]+", "_", public_key.strip().lower()).strip("_")
        if normalized == "result" and allow_safe_result:
            sanitized[public_key] = _sanitize_safe_result(item)
            continue
        if normalized in {"path", "source_path"}:
            if public_logical_path(item) is None:
                continue
        elif normalized in _PUBLIC_DENY_KEYS or normalized.endswith(_PUBLIC_PATH_KEY_SUFFIXES):
            continue
        sanitized[public_key] = _sanitize_public(
            item,
            allow_safe_result=allow_safe_result,
            _field_name=normalized,
        )
    return sanitized


def validate_runtime_identifier(value: str, *, name: str = "标识") -> str:
    """校验 Working State、检查点和审计关联使用的逻辑标识，不回显非法值。"""
    candidate = value.strip().lower()
    if not RUNTIME_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"{name} 无效")
    return candidate


def validate_task_identifier(value: str) -> str:
    """校验编排任务 ID；仅接受服务生成的 32 位十六进制标识。"""
    candidate = value.strip().lower()
    if not TASK_ID_PATTERN.fullmatch(candidate):
        raise ValueError("task_id 无效")
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


def require_mutation_request(request: Request) -> None:
    """校验写请求的 JSON、浏览器标记和同源边界，不接受任意跨站 POST。"""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("请求格式无效")
    if request.headers.get("X-AIKB-Request") != "1":
        raise ValueError("请求标记无效")
    origin = request.headers.get("origin")
    host = request.headers.get("host", "").strip().lower()
    if not origin or not host:
        raise ValueError("请求来源无效")
    host_parts = urlsplit(f"//{host}")
    host_name = (host_parts.hostname or "").lower()
    if host_name not in {"localhost", "127.0.0.1"}:
        raise ValueError("请求来源无效")
    parsed = urlsplit(origin)
    origin_host = (parsed.netloc or "").lower()
    origin_name = (parsed.hostname or "").lower()
    if origin_name not in {"localhost", "127.0.0.1"}:
        raise ValueError("请求来源无效")
    same_origin = (
        parsed.scheme == request.url.scheme
        and origin_name == host_name
        and (parsed.port or (443 if parsed.scheme == "https" else 80))
        == (host_parts.port or (443 if request.url.scheme == "https" else 80))
    )
    dev_mode = os.environ.get("AIKB_WEB_DEV_ORIGIN") == "1" or os.environ.get("AIKB_WEB_DEV_MODE") == "1"
    explicit_dev_origin = origin in {"http://localhost:5173", "http://127.0.0.1:5173"}
    if not same_origin and not (dev_mode and explicit_dev_origin):
        raise ValueError("请求来源无效")


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
