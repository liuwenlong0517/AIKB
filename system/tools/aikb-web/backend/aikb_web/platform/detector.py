"""当前平台检测与适配器选择。"""

from __future__ import annotations

import platform

from .base import PlatformState
from .windows import windows_state


def platform_state() -> PlatformState:
    """返回当前平台状态；macOS 只表达预留，不加载未验证实现。"""
    system = platform.system().lower()
    architecture = platform.machine().lower()
    if system == "windows":
        return windows_state(architecture)
    if system == "darwin":
        return PlatformState(
            platform="macos",
            architecture=architecture,
            supported=False,
            reason="macOS implementation is reserved but not yet available",
        )
    return PlatformState(
        platform=system or "unknown",
        architecture=architecture,
        supported=False,
        reason="platform is not implemented",
    )
