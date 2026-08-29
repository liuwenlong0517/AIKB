"""系统状态和平台能力接口。"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from aikb_web.platform import platform_state

from .common import success


router = APIRouter(prefix="/system", tags=["system"])


def _git_value(root: Path, args: list[str]) -> str | None:
    """读取 Git 的单个公开字段；命令失败时返回 None，不把错误传给客户端。"""
    try:
        value = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value or None


def _safe_repo_state(root: Path | None) -> dict[str, Any]:
    """只公开分支和短提交，不返回仓库物理路径或任意 Git 输出。"""
    if root is None:
        return {"available": False}
    commit = _git_value(root, ["rev-parse", "--short", "HEAD"])
    branch = _git_value(root, ["branch", "--show-current"])
    return {"available": bool(commit or branch), "branch": branch, "short_commit": commit}


def _settings(request: Request) -> Any | None:
    """读取注入网关的设置对象；测试网关没有设置时安全降级。"""
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    return getattr(gateway, "settings", None)


@router.get("/info")
def system_info(request: Request) -> dict[str, Any]:
    """返回平台、双仓 Git 摘要和索引状态等不敏感运行信息。"""
    settings = _settings(request)
    control_root = getattr(settings, "repo_root", None)
    knowledge_root = getattr(settings, "knowledge_root", None)
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    index: dict[str, Any] = {"available": False}
    try:
        overview = gateway.overview() if gateway is not None else {}
        candidate = overview.get("index") if isinstance(overview, dict) else None
        if isinstance(candidate, dict):
            index = {key: candidate.get(key) for key in ("available", "tokenizer", "rebuilt") if key in candidate}
    except Exception:
        pass
    return success(
        {
            "platform": {"name": platform.system().lower(), "architecture": platform.machine().lower()},
            "python": {"version": platform.python_version()},
            "repositories": {"control": _safe_repo_state(control_root), "knowledge": _safe_repo_state(knowledge_root)},
            "index": index,
        },
        request,
    )


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    """返回平台能力模型；macOS 明确保留但不伪装成已支持。"""
    current_platform = platform_state()
    return success(
        {
            "platform": current_platform.public_dict(),
            "read_only": True,
            "capabilities": [
                {"id": "knowledge.read", "supported": True},
                {"id": "knowledge.search", "supported": True},
                {"id": "controlled.actions", "supported": False, "reason": "not available in phase 1"},
            ],
        },
        request,
    )
