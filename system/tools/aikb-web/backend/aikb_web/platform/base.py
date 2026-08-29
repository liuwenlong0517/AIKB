"""平台能力的公共数据契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlatformState:
    """描述平台身份和当前实现状态，不包含环境变量或物理路径。"""

    platform: str
    architecture: str
    supported: bool
    reason: str | None = None

    def public_dict(self) -> dict[str, Any]:
        """转换为 API 可公开字段，并省略空原因。"""
        return {key: value for key, value in asdict(self).items() if value is not None}
