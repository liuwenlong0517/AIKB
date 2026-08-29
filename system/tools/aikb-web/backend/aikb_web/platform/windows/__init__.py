"""Windows 第一阶段平台信息实现。"""

from __future__ import annotations

from ..base import PlatformState
from .commands import CommandError, CommandSpec, build_action_commands, build_child_environment
from .executor import ExecutionError, ExecutionResult, WindowsExecutor


def windows_state(architecture: str) -> PlatformState:
    """声明 Windows 上已验证的只读 Web 能力。"""
    return PlatformState(platform="windows", architecture=architecture, supported=True)


__all__ = [
    "CommandError", "CommandSpec", "ExecutionError", "ExecutionResult", "WindowsExecutor",
    "build_action_commands", "build_child_environment", "windows_state",
]
