"""阶段 4B 公共维护平台 SPI 与平台能力声明。

这里定义的是跨平台的安全数据契约和适配器协议。协议参数只接受目标/变更/步骤
等逻辑 ID 或受约束模型；本模块不解析路径、不执行命令、不写配置，也不声称
Windows 已经具备真实安装实现。具体 Windows 读写适配器由后续波次实现。
"""

from __future__ import annotations

import platform as host_platform
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.maintenance_targets import (
    MAINTENANCE_TARGET_REGISTRY,
    MAINTENANCE_STEP_IDS,
    MAINTENANCE_STATUSES,
    MaintenanceStatus,
    MaintenanceTargetError,
    MaintenanceTargetStatus,
    validate_logical_id,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAINTENANCE_OUTCOME_CODES = (
    "applied",
    "failed",
    "rolled_back",
    "recovery_required",
)
MAINTENANCE_PLATFORM_REASON_CODES = (
    "none",
    "reserved_not_implemented",
    "unsupported_platform",
)
MAINTENANCE_ADAPTER_IDS = ("windows-maintenance", "macos-maintenance")
_SAFE_PLATFORM_TOKEN_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_SAFE_ARCHITECTURE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _validate_change_id(change_id: str) -> str:
    """复用逻辑 ID 规则校验服务端生成的变更编号，拒绝目录穿越。"""

    return validate_logical_id(change_id, "change_id")


@dataclass(frozen=True)
class MaintenanceStep:
    """一个仅含语义 ID 的维护步骤；不承载命令、路径或配置正文。"""

    step_id: str

    def __post_init__(self) -> None:
        """确保步骤 ID 可安全进入任务和事务模型。"""

        if not isinstance(self.step_id, str):
            raise MaintenanceTargetError("step_id 必须是字符串")
        if self.step_id not in MAINTENANCE_STEP_IDS:
            raise MaintenanceTargetError("step_id 未声明")

    def public_dict(self) -> dict[str, str]:
        """返回步骤语义投影。"""

        return {"step_id": self.step_id}


@dataclass(frozen=True)
class MaintenancePlan:
    """无副作用规划结果，只描述逻辑叶子、语义步骤和摘要指纹。"""

    target_id: str
    steps: tuple[MaintenanceStep, ...]
    logical_leaves: tuple[str, ...]
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    preview_digest: str | None = None

    def __post_init__(self) -> None:
        """校验规划模型不含物理定位和非安全自由参数。"""

        target = MAINTENANCE_TARGET_REGISTRY.get(self.target_id)
        if not self.steps or not self.logical_leaves:
            raise MaintenanceTargetError("维护规划必须包含步骤和逻辑叶子")
        if any(not isinstance(step, MaintenanceStep) for step in self.steps):
            raise MaintenanceTargetError("维护规划步骤类型无效")
        if tuple(step.step_id for step in self.steps) != target.steps:
            raise MaintenanceTargetError("维护规划步骤必须与目标静态定义完全一致")
        if tuple(self.logical_leaves) != target.logical_leaves:
            raise MaintenanceTargetError("维护规划叶子必须与目标静态定义完全一致")
        for field_name in ("before_fingerprint", "after_fingerprint", "preview_digest"):
            fingerprint = getattr(self, field_name)
            if fingerprint is None or _SHA256_RE.fullmatch(fingerprint) is None:
                raise MaintenanceTargetError(f"{field_name} 必须是必填 SHA-256")

    def public_dict(self) -> dict[str, object]:
        """返回可公开的规划投影，不暴露正文、路径或底层异常。"""

        return {
            "target_id": self.target_id,
            "steps": [step.public_dict() for step in self.steps],
            "logical_leaves": list(self.logical_leaves),
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "preview_digest": self.preview_digest,
        }


@dataclass(frozen=True)
class MaintenanceStepResult:
    """单个步骤的安全结果；仅保留通过状态和语义说明。"""

    change_id: str
    target_id: str
    step_id: str
    succeeded: bool
    outcome_code: str

    def __post_init__(self) -> None:
        """校验结果中的所有标识均为逻辑 ID。"""

        _validate_change_id(self.change_id)
        target = MAINTENANCE_TARGET_REGISTRY.get(self.target_id)
        if not isinstance(self.step_id, str):
            raise MaintenanceTargetError("step_id 必须是字符串")
        # rollback 是所有目标共享的补偿步骤；其余步骤必须属于当前目标，避免
        # 执行器把 Agent 写入步骤错误投影到环境变量事务等其他目标。
        if self.step_id not in {*target.steps, "rollback"}:
            raise MaintenanceTargetError("step_id 与维护目标不匹配")
        if not isinstance(self.succeeded, bool):
            raise MaintenanceTargetError("succeeded 必须是布尔值")
        if self.outcome_code not in MAINTENANCE_OUTCOME_CODES:
            raise MaintenanceTargetError("outcome_code 未声明")
        successful_outcomes = {"applied", "rolled_back"}
        if self.succeeded != (self.outcome_code in successful_outcomes):
            raise MaintenanceTargetError("succeeded 与 outcome_code 不匹配")

    def public_dict(self) -> dict[str, object]:
        """生成安全步骤结果投影。"""

        return {
            "change_id": self.change_id,
            "target_id": self.target_id,
            "step_id": self.step_id,
            "succeeded": self.succeeded,
            "outcome_code": self.outcome_code,
        }


@dataclass(frozen=True)
class MaintenanceVerification:
    """维护后验证的安全结果，不保存探针命令或原始输出。"""

    change_id: str
    target_id: str
    status: MaintenanceStatus | str
    restart_required: bool = False
    after_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """确保验证状态属于固定枚举且不会携带未约束信息。"""

        _validate_change_id(self.change_id)
        MAINTENANCE_TARGET_REGISTRY.get(self.target_id)
        status = self.status.value if isinstance(self.status, MaintenanceStatus) else self.status
        if status not in MAINTENANCE_STATUSES:
            raise MaintenanceTargetError("验证状态无效")
        if not isinstance(self.restart_required, bool):
            raise MaintenanceTargetError("restart_required 必须是布尔值")
        if self.after_fingerprint is not None and _SHA256_RE.fullmatch(self.after_fingerprint) is None:
            raise MaintenanceTargetError("after_fingerprint 必须是 SHA-256")
        if status == "restart_required" and not self.restart_required:
            raise MaintenanceTargetError("restart_required 状态必须要求重启")
        if status != "restart_required" and self.restart_required:
            raise MaintenanceTargetError("只有 restart_required 状态可以要求重启")
        object.__setattr__(self, "status", status)

    def public_dict(self) -> dict[str, object]:
        """返回验证状态和重启提示，不返回探针细节。"""

        return {
            "change_id": self.change_id,
            "target_id": self.target_id,
            "status": self.status,
            "restart_required": self.restart_required,
            "after_fingerprint": self.after_fingerprint,
        }

    to_public_dict = public_dict


@dataclass(frozen=True)
class MaintenanceRecoveryResult:
    """崩溃恢复的安全结果，只表达逻辑变更的恢复状态。"""

    change_id: str
    outcome_code: str

    def __post_init__(self) -> None:
        """校验恢复结果中的变更 ID，避免把事务目录信息带入模型。"""

        _validate_change_id(self.change_id)
        if self.outcome_code not in {"rolled_back", "recovery_required"}:
            raise MaintenanceTargetError("恢复 outcome_code 无效")

    def public_dict(self) -> dict[str, str]:
        """返回安全恢复投影。"""

        return {"change_id": self.change_id, "outcome_code": self.outcome_code}

    to_public_dict = public_dict


@runtime_checkable
class MaintenancePlatformAdapter(Protocol):
    """维护平台 SPI；实现方必须自行执行平台边界和重解析点校验。

    所有方法都只接收逻辑目标 ID、服务端变更 ID 和安全模型。适配器内部可以
    解析已经固定、已验证的用户配置位置，但不得把物理路径暴露到这些参数、
    返回值、任务事实或公开投影中。
    """

    def inspect(self, target_id: str) -> MaintenanceTargetStatus:
        """只读检查目标状态；不得创建临时文件、备份、审计 probe 或事务。"""

    def plan(self, target_id: str, inspection: MaintenanceTargetStatus) -> MaintenancePlan:
        """根据已检查状态生成无副作用计划；冲突/损坏状态不得猜测修复。"""

    def apply_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """应用一个服务端固定步骤；不得从浏览器接收路径、命令或正文。"""

    def verify(self, change_id: str, target_id: str) -> MaintenanceVerification:
        """验证已应用目标并返回安全状态；原始探针输出不进入返回模型。"""

    def rollback_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """按事务相反顺序补偿一个步骤；无法证明安全恢复时应报告恢复所需状态。"""

    def recover(self, change_id: str) -> MaintenanceRecoveryResult:
        """从持久化事务事实恢复未完成变更；第三方修改时不得静默覆盖。"""


@dataclass(frozen=True)
class MaintenancePlatformCapabilities:
    """维护能力的公开平台声明，不等价于真实读写实现已完成。"""

    platform: str
    architecture: str
    supported: bool
    reason_code: str
    adapter: str | None = None

    def __post_init__(self) -> None:
        """校验平台声明的安全 token 及 supported/adapter/原因码组合。"""

        if not isinstance(self.platform, str) or _SAFE_PLATFORM_TOKEN_RE.fullmatch(self.platform) is None:
            raise MaintenanceTargetError("platform 必须是安全逻辑值")
        if not isinstance(self.architecture, str) or _SAFE_ARCHITECTURE_RE.fullmatch(self.architecture) is None:
            raise MaintenanceTargetError("architecture 必须是安全逻辑值")
        if not isinstance(self.supported, bool):
            raise MaintenanceTargetError("supported 必须是布尔值")
        if self.reason_code not in MAINTENANCE_PLATFORM_REASON_CODES:
            raise MaintenanceTargetError("reason_code 未声明")
        if not self.supported:
            if self.adapter is not None:
                raise MaintenanceTargetError("不支持的平台不得声明 adapter")
            if self.reason_code == "none":
                raise MaintenanceTargetError("不支持的平台必须提供原因码")
        elif self.reason_code != "none" or self.adapter not in MAINTENANCE_ADAPTER_IDS:
            raise MaintenanceTargetError("支持的平台必须使用固定 adapter 和 none 原因码")

    def public_dict(self) -> dict[str, object]:
        """返回不含路径、命令和实现内部信息的平台能力投影。"""

        value: dict[str, object] = {
            "platform": self.platform,
            "architecture": self.architecture,
            "supported": self.supported,
        }
        value["reason_code"] = self.reason_code
        if self.adapter is not None:
            value["adapter"] = self.adapter
        return value

    to_public_dict = public_dict


def windows_maintenance_capabilities(architecture: str | None = None) -> MaintenancePlatformCapabilities:
    """声明 Windows SPI 预留但尚未实现；本函数不执行任何 Windows 操作。"""

    return MaintenancePlatformCapabilities(
        platform="windows",
        architecture=(architecture or host_platform.machine()).lower(),
        supported=False,
        reason_code="reserved_not_implemented",
        adapter=None,
    )


def macos_maintenance_capabilities(architecture: str | None = None) -> MaintenancePlatformCapabilities:
    """明确声明 macOS 尚未支持，避免复用 Windows 路径或配置假设。"""

    return MaintenancePlatformCapabilities(
        platform="macos",
        architecture=(architecture or host_platform.machine()).lower(),
        supported=False,
        reason_code="reserved_not_implemented",
        adapter=None,
    )


def maintenance_platform_capabilities(
    system: str | None = None,
    architecture: str | None = None,
) -> MaintenancePlatformCapabilities:
    """根据平台名称返回安全能力声明；未知平台同样保持不支持。"""

    normalized = (system or host_platform.system()).lower()
    if normalized == "windows":
        return windows_maintenance_capabilities(architecture)
    if normalized in {"darwin", "macos"}:
        return macos_maintenance_capabilities(architecture)
    return MaintenancePlatformCapabilities(
        platform=normalized or "unknown",
        architecture=(architecture or host_platform.machine()).lower(),
        supported=False,
        reason_code="unsupported_platform",
    )


__all__ = [
    "MaintenancePlatformAdapter",
    "MaintenancePlatformCapabilities",
    "MAINTENANCE_ADAPTER_IDS",
    "MAINTENANCE_OUTCOME_CODES",
    "MAINTENANCE_PLATFORM_REASON_CODES",
    "MaintenancePlan",
    "MaintenanceRecoveryResult",
    "MaintenanceStep",
    "MaintenanceStepResult",
    "MaintenanceVerification",
    "macos_maintenance_capabilities",
    "maintenance_platform_capabilities",
    "windows_maintenance_capabilities",
]
