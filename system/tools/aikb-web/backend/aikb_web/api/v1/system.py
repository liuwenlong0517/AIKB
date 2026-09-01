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
    # 只公开是否有未提交变更，不返回文件名、Git 原文或仓库路径。
    status = _git_value(root, ["status", "--porcelain"])
    return {"available": bool(commit or branch), "branch": branch, "short_commit": commit, "dirty": bool(status)}


def _settings(request: Request) -> Any | None:
    """读取注入网关的设置对象；测试网关没有设置时安全降级。"""
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    return getattr(gateway, "settings", None)


@router.get("/info")
def system_info(request: Request) -> dict[str, Any]:
    """返回平台、双仓 Git 摘要、索引和规则恢复提示等不敏感运行信息。"""
    settings = _settings(request)
    gateway = getattr(request.app.state, "knowledge_gateway", None)
    index: dict[str, Any] = {"available": False}
    repositories: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        overview = gateway.overview() if gateway is not None else {}
        candidate = overview.get("index") if isinstance(overview, dict) else None
        if isinstance(candidate, dict):
            index = {key: candidate.get(key) for key in ("available", "tokenizer", "rebuilt") if key in candidate}
    except Exception:
        warnings.append("index_unavailable")
    # 双仓语义摘要由共享 WorkStateStore 提供；旧网关或测试替身不具备该
    # 扩展时才使用本地 Git 的有限字段作为兼容降级，仍不读取文件内容。
    repository_method = getattr(gateway, "web_repository_summary", None)
    if callable(repository_method):
        try:
            raw = repository_method()
            for item in raw.get("repositories", []) if isinstance(raw, dict) else []:
                if not isinstance(item, dict) or item.get("role") not in {"control", "knowledge"}:
                    continue
                repositories[item["role"]] = {
                    "available": bool(item.get("available")),
                    "branch": item.get("branch"),
                    "short_commit": item.get("revision"),
                    "dirty": bool(item.get("dirty")),
                }
            if isinstance(raw, dict) and raw.get("status") == "degraded":
                warnings.append("repositories_unavailable")
        except Exception:
            warnings.append("repositories_unavailable")
    if not repositories:
        control_root = getattr(settings, "repo_root", None)
        knowledge_root = getattr(settings, "knowledge_root", None)
        repositories = {"control": _safe_repo_state(control_root), "knowledge": _safe_repo_state(knowledge_root)}
    rule_apply = getattr(request.app.state, "rule_apply_service", None)
    rule_status = rule_apply.public_status() if callable(getattr(rule_apply, "public_status", None)) else {"available": False}
    return success(
        {
            "platform": {"name": platform.system().lower(), "architecture": platform.machine().lower()},
            "python": {"version": platform.python_version()},
            "repositories": repositories,
            "index": index,
            "rule_writes": rule_status,
        },
        request,
        degraded=bool(warnings),
        warnings=list(dict.fromkeys(warnings)),
    )


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    """返回平台能力模型；macOS 明确保留但不伪装成已支持。"""
    current_platform = platform_state()
    actions_supported = bool(getattr(request.app.state, "platform_action_available", False))
    return success(
        {
            "platform": current_platform.public_dict(),
            "read_only": True,
            "capabilities": [
                {"id": "knowledge.read", "supported": True},
                {"id": "knowledge.search", "supported": True},
                {"id": "manuals.read", "supported": True},
                {"id": "runtime.work_state.read", "supported": True},
                {"id": "runtime.archive.read", "supported": True},
                {"id": "runtime.checkpoint.read", "supported": True},
                {"id": "audit.read", "supported": True},
                {"id": "knowledge.write", "supported": False, "reason": "read_only"},
                {"id": "runtime.work_state.write", "supported": False, "reason": "read_only"},
                {
                    "id": "controlled.actions", "supported": actions_supported,
                    **({} if actions_supported else {"reason": "platform_or_prerequisites_unavailable"}),
                },
                {"id": "shell.execute", "supported": False, "reason": "not_supported"},
                {"id": "git.write", "supported": False, "reason": "not_supported"},
                {"id": "network.access", "supported": False, "reason": "not_supported"},
            ],
        },
        request,
    )
