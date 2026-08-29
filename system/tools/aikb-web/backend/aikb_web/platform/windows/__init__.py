"""Windows 第一阶段平台信息实现。"""

from __future__ import annotations

from ..base import PlatformState


def windows_state(architecture: str) -> PlatformState:
    """声明 Windows 上已验证的只读 Web 能力。"""
    return PlatformState(platform="windows", architecture=architecture, supported=True)


__all__ = ["windows_state"]
